/**
 * Signaling + WebRTC setup: get from a room id to an open DataChannel.
 * Browser counterpart of apps/cli/connection.py.
 */
import { type Channel, ConnectionClosedError } from "./transfer";

/** From negotiation start to an open DataChannel. Matches the CLI. */
const CONNECT_TIMEOUT_MS = 30_000;
/** Max wait for ICE candidate gathering before sending SDP anyway. */
const GATHER_TIMEOUT_MS = 5_000;
/** drain() blocks above HIGH and resumes at LOW — same as packages/rtc. */
const HIGH_WATER_BYTES = 1024 * 1024;
const LOW_WATER_BYTES = 256 * 1024;

export class ConnectError extends Error {}

export type ConnectPhase = "signaling" | "waiting" | "connecting";

// ── HTTP ────────────────────────────────────────────────────────────

export async function createRoom(): Promise<string> {
  const resp = await fetch("/rooms", { method: "POST" });
  if (resp.status === 429) throw new ConnectError("Too many rooms created. Wait a minute.");
  if (!resp.ok) throw new ConnectError(`Couldn't create a room (HTTP ${resp.status}).`);
  return ((await resp.json()) as { room_id: string }).room_id;
}

async function fetchIceServers(): Promise<RTCIceServer[]> {
  const resp = await fetch("/config");
  if (!resp.ok) throw new ConnectError(`Couldn't load server config (HTTP ${resp.status}).`);
  return ((await resp.json()) as { ice_servers: RTCIceServer[] }).ice_servers;
}

// ── DataChannel adapter ─────────────────────────────────────────────

/** RTCDataChannel with the send/drain/recv interface the session uses. */
export class PeerChannel implements Channel {
  private lowWaiters: (() => void)[] = [];

  constructor(
    readonly dc: RTCDataChannel,
    readonly pc: RTCPeerConnection,
  ) {
    dc.binaryType = "arraybuffer";
    dc.bufferedAmountLowThreshold = LOW_WATER_BYTES;
    dc.addEventListener("bufferedamountlow", () => this.wake());
    dc.addEventListener("close", () => this.wake());
  }

  send(data: string | Uint8Array): void {
    if (this.dc.readyState !== "open") throw new ConnectionClosedError();
    if (typeof data === "string") this.dc.send(data);
    // The cast narrows away SharedArrayBuffer-backed views, which we never create
    else this.dc.send(data as Uint8Array<ArrayBuffer>);
  }

  async drain(): Promise<void> {
    for (;;) {
      if (this.dc.readyState !== "open") throw new ConnectionClosedError();
      if (this.dc.bufferedAmount <= HIGH_WATER_BYTES) return;
      await new Promise<void>((resolve) => this.lowWaiters.push(resolve));
    }
  }

  close(): void {
    this.dc.close();
    this.pc.close();
  }

  private wake(): void {
    const waiters = this.lowWaiters;
    this.lowWaiters = [];
    for (const resolve of waiters) resolve();
  }
}

// ── Connect ─────────────────────────────────────────────────────────

export interface Connection {
  channel: PeerChannel;
  /** The signaling socket stays open for peer_left notices; P2P doesn't need it. */
  ws: WebSocket;
}

/**
 * Join `roomId` and resolve once the DataChannel is open.
 *
 * Waits indefinitely for the other peer, then allows CONNECT_TIMEOUT_MS.
 * SDP is sent once ICE gathering completes, with candidates inline — no
 * trickle ICE, because the Python client (aiortc) neither sends nor
 * accepts trickled candidates (docs/decisions.md D35).
 */
export async function connect(
  roomId: string,
  isCreator: boolean,
  onPhase: (phase: ConnectPhase) => void,
  signal: AbortSignal,
): Promise<Connection> {
  const iceServers = await fetchIceServers();
  // Bail before opening the socket: a cancelled attempt that reached the
  // server would hold a room slot (React StrictMode runs effects twice).
  if (signal.aborted) throw new ConnectError("Cancelled.");
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}/rooms/${encodeURIComponent(roomId)}/ws`);
  const pc = new RTCPeerConnection({ iceServers });

  return new Promise<Connection>((resolve, reject) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const fail = (message: string) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      ws.close();
      pc.close();
      reject(new ConnectError(message));
    };

    signal.addEventListener("abort", () => fail("Cancelled."));

    const onChannel = (dc: RTCDataChannel) => {
      const open = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve({ channel: new PeerChannel(dc, pc), ws });
      };
      if (dc.readyState === "open") open();
      else dc.addEventListener("open", open);
    };

    const startNegotiation = () => {
      if (timer !== undefined) return;
      onPhase("connecting");
      timer = setTimeout(() => {
        fail(
          pc.iceConnectionState === "failed"
            ? directFailedMessage
            : "Timed out establishing the connection.",
        );
      }, CONNECT_TIMEOUT_MS);
    };

    pc.addEventListener("iceconnectionstatechange", () => {
      if (pc.iceConnectionState === "failed") fail(directFailedMessage);
    });
    pc.addEventListener("datachannel", (event) => onChannel(event.channel));

    const sendDescription = async (description: RTCSessionDescriptionInit) => {
      await pc.setLocalDescription(description);
      await iceGatheringComplete(pc);
      const local = pc.localDescription!;
      ws.send(JSON.stringify({ type: local.type, sdp: local.sdp }));
    };

    const onSignal = async (msg: { type?: string; sdp?: string; reason?: string }) => {
      switch (msg.type) {
        case "hello":
          onPhase("waiting");
          break;
        case "peer_joined":
          if (isCreator) {
            startNegotiation();
            onChannel(pc.createDataChannel("whoosh"));
            await sendDescription(await pc.createOffer());
          }
          break;
        case "offer":
          startNegotiation();
          await pc.setRemoteDescription({ type: "offer", sdp: msg.sdp });
          await sendDescription(await pc.createAnswer());
          break;
        case "answer":
          await pc.setRemoteDescription({ type: "answer", sdp: msg.sdp });
          break;
        case "error":
          console.error("signaling server:", msg.reason);
          break;
      }
    };

    onPhase("signaling");
    ws.addEventListener("message", (event) => {
      let msg: unknown;
      try {
        msg = JSON.parse(event.data as string);
      } catch {
        return;
      }
      onSignal(msg as Parameters<typeof onSignal>[0]).catch((err: unknown) =>
        fail(`WebRTC negotiation failed: ${(err as Error).message}`),
      );
    });
    ws.addEventListener("close", (event) => {
      const why = event.reason ? `: ${event.reason}` : ".";
      fail(
        timer === undefined
          ? `Signaling closed before a peer arrived${why}`
          : `Signaling closed before the connection opened${why}`,
      );
    });
  });
}

const directFailedMessage =
  "Direct connection failed: no network path between the peers. One side is likely " +
  "behind a strict NAT or firewall (relay servers aren't supported yet).";

/** Resolve when ICE gathering completes, or after GATHER_TIMEOUT_MS with
 * whatever candidates were found by then. */
function iceGatheringComplete(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      pc.removeEventListener("icegatheringstatechange", check);
      resolve();
    };
    const check = () => {
      if (pc.iceGatheringState === "complete") done();
    };
    const timer = setTimeout(done, GATHER_TIMEOUT_MS);
    pc.addEventListener("icegatheringstatechange", check);
  });
}
