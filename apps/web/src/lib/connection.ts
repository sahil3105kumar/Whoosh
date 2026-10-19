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
/** A connection "disconnected" this long is treated as gone. ICE recovers
 * from brief blips by itself; a peer that crashed never comes back, and its
 * DataChannel would otherwise never fire "close". */
const DISCONNECT_GRACE_MS = 8_000;

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
  private closeListeners: (() => void)[] = [];
  private closed = false;

  get isClosed(): boolean {
    return this.closed;
  }
  private disconnectTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(
    readonly dc: RTCDataChannel,
    readonly pc: RTCPeerConnection,
  ) {
    dc.binaryType = "arraybuffer";
    dc.bufferedAmountLowThreshold = LOW_WATER_BYTES;
    dc.addEventListener("bufferedamountlow", () => this.wake());
    dc.addEventListener("close", () => this.markClosed());
    pc.addEventListener("connectionstatechange", () => this.watchConnection());
  }

  /** Called once when the channel ends: closed by either side, or the peer
   * stopped responding (see DISCONNECT_GRACE_MS). */
  onClose(listener: () => void): void {
    if (this.closed) listener();
    else this.closeListeners.push(listener);
  }

  send(data: string | Uint8Array): void {
    if (this.closed || this.dc.readyState !== "open") throw new ConnectionClosedError();
    if (typeof data === "string") this.dc.send(data);
    // The cast narrows away SharedArrayBuffer-backed views, which we never create
    else this.dc.send(data as Uint8Array<ArrayBuffer>);
  }

  async drain(): Promise<void> {
    for (;;) {
      if (this.closed || this.dc.readyState !== "open") throw new ConnectionClosedError();
      if (this.dc.bufferedAmount <= HIGH_WATER_BYTES) return;
      await new Promise<void>((resolve) => this.lowWaiters.push(resolve));
    }
  }

  close(): void {
    this.dc.close();
    this.pc.close();
    this.markClosed();
  }

  private watchConnection(): void {
    const state = this.pc.connectionState;
    if (state === "failed" || state === "closed") {
      this.markClosed();
    } else if (state === "disconnected") {
      this.disconnectTimer ??= setTimeout(() => this.markClosed(), DISCONNECT_GRACE_MS);
    } else {
      clearTimeout(this.disconnectTimer);
      this.disconnectTimer = undefined;
    }
  }

  private markClosed(): void {
    if (this.closed) return;
    this.closed = true;
    clearTimeout(this.disconnectTimer);
    this.wake();
    if (this.pc.connectionState !== "closed") this.pc.close();
    for (const listener of this.closeListeners) listener();
    this.closeListeners = [];
  }

  private wake(): void {
    const waiters = this.lowWaiters;
    this.lowWaiters = [];
    for (const resolve of waiters) resolve();
  }
}

// ── Connect ─────────────────────────────────────────────────────────

/** How long reconnect() keeps trying after the connection drops. */
const RECONNECT_TIMEOUT_MS = 60_000;
/** WebSocket close code the server uses for "room not found / expired / full". */
const POLICY_VIOLATION = 1008;

/** Turn the server's WebSocket close reason into advice for a person. */
export function explainClose(reason: string, roomId: string): string {
  if (reason.startsWith("No such room")) {
    return `Room ${roomId} doesn't exist. Check the code; rooms also expire 10 minutes after they're created.`;
  }
  if (reason.startsWith("Room expired")) return `Room ${roomId} has expired. Ask for a new code.`;
  if (reason.startsWith("Room is full")) return `Room ${roomId} already has two people in it.`;
  return reason;
}

interface Signal {
  type?: string;
  sdp?: string;
  reason?: string;
  peer_id?: string;
  peers?: string[];
}

/**
 * A room's connection to the other peer, re-established when it drops
 * (#19, docs/decisions.md D50) — the browser side of apps/cli/connection.py.
 *
 * Owns the signaling WebSocket (reconnecting with its peer_id so it keeps
 * its slot) and the current RTCPeerConnection. When the peer-to-peer
 * connection dies, the room creator re-offers as soon as both sides are
 * reachable through signaling; the joiner accepts any new offer.
 *
 * SDP is sent once ICE gathering completes, with candidates inline — no
 * trickle ICE, because the Python client (aiortc) neither sends nor accepts
 * trickled candidates (D35).
 */
export class Link {
  private iceServers: RTCIceServer[] = [];
  private ws?: WebSocket;
  private peerId?: string;
  private pc?: RTCPeerConnection;
  private ready?: PeerChannel;
  private otherSeen = false;
  private negotiationStarted?: number;
  private lastIceFailed = false;
  private fatal?: ConnectError;
  private closing = false;
  private backoffMs = 500;
  private waiters: (() => void)[] = [];

  constructor(
    readonly roomId: string,
    readonly isCreator: boolean,
    private readonly onPhase: (phase: ConnectPhase) => void,
  ) {}

  /** First connection: waits indefinitely for the other person, then
   * CONNECT_TIMEOUT_MS for the connection itself. */
  async open(signal: AbortSignal): Promise<PeerChannel> {
    this.iceServers = await fetchIceServers();
    // Bail before opening the socket: a cancelled attempt that reached the
    // server would hold a room slot (React StrictMode runs effects twice).
    if (signal.aborted) throw new ConnectError("Cancelled.");
    signal.addEventListener("abort", () => this.close());
    this.onPhase("signaling");
    this.connectSignaling();
    return this.waitReady(true);
  }

  /** After the connection closed: wait for a new one. */
  reconnect(timeoutMs = RECONNECT_TIMEOUT_MS): Promise<PeerChannel> {
    if (this.isCreator) setTimeout(() => this.renegotiate(), 1000);
    return this.waitReady(false, timeoutMs);
  }

  /** Abandon the current connection (it stalled), so a new one is made. */
  drop(): void {
    this.ready?.close();
    this.pc?.close();
  }

  close(): void {
    this.closing = true;
    this.drop();
    this.ws?.close();
    this.pulse();
  }

  // ── Waiting ───────────────────────────────────────────────────────

  private pulse(): void {
    const waiters = this.waiters;
    this.waiters = [];
    for (const wake of waiters) wake();
  }

  private async waitReady(first: boolean, timeoutMs?: number): Promise<PeerChannel> {
    let deadline = timeoutMs === undefined ? undefined : Date.now() + timeoutMs;
    for (;;) {
      if (this.ready && !this.ready.isClosed) return this.ready;
      if (this.fatal) throw this.fatal;
      if (this.closing) throw new ConnectError("Cancelled.");
      if (first && deadline === undefined && this.negotiationStarted !== undefined) {
        deadline = this.negotiationStarted + CONNECT_TIMEOUT_MS;
      }
      const remaining = deadline === undefined ? undefined : deadline - Date.now();
      if (remaining !== undefined && remaining <= 0) {
        if (this.lastIceFailed) throw new ConnectError(directFailedMessage);
        throw new ConnectError(
          first ? "Timed out establishing the connection." : "Couldn't reconnect to the other person.",
        );
      }
      await new Promise<void>((resolve) => {
        this.waiters.push(resolve);
        if (remaining !== undefined) setTimeout(resolve, remaining);
      });
    }
  }

  // ── Signaling ─────────────────────────────────────────────────────

  private connectSignaling(): void {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const query = this.peerId ? `?peer_id=${encodeURIComponent(this.peerId)}` : "";
    const ws = new WebSocket(
      `${scheme}://${location.host}/rooms/${encodeURIComponent(this.roomId)}/ws${query}`,
    );
    this.ws = ws;
    ws.addEventListener("message", (event) => {
      let msg: Signal;
      try {
        msg = JSON.parse(event.data as string) as Signal;
      } catch {
        return;
      }
      this.onSignal(msg).catch((err: unknown) =>
        console.error("WebRTC negotiation failed", err),
      );
    });
    ws.addEventListener("close", (event) => {
      if (this.ws === ws) this.ws = undefined;
      if (this.closing) return;
      if (event.code === POLICY_VIOLATION) {
        // Room gone, expired or full: reconnecting can't help. A live P2P
        // connection is unaffected; it just can't be re-made.
        this.setFatal(explainClose(event.reason, this.roomId));
        return;
      }
      if (this.peerId === undefined && !this.ready) {
        this.setFatal("Can't reach the signaling server.");
        return;
      }
      setTimeout(() => this.connectSignaling(), this.backoffMs);
      this.backoffMs = Math.min(this.backoffMs * 2, 10_000);
    });
  }

  private setFatal(message: string): void {
    this.fatal = new ConnectError(message);
    this.pulse();
  }

  private sendSignal(msg: object): void {
    if (this.ws?.readyState !== WebSocket.OPEN) throw new Error("signaling is down");
    this.ws.send(JSON.stringify(msg));
  }

  private async onSignal(msg: Signal): Promise<void> {
    switch (msg.type) {
      case "hello":
        this.peerId = msg.peer_id;
        if (msg.peers?.length) this.otherSeen = true; // they connected before us
        this.backoffMs = 500;
        if (!this.ready) this.onPhase(this.negotiationStarted ? "connecting" : "waiting");
        // Back on signaling with the P2P link down: try again now
        if (this.isCreator && this.otherSeen && !this.connected()) await this.negotiate();
        break;
      case "peer_joined":
        this.otherSeen = true;
        if (this.isCreator && !this.connected()) await this.negotiate();
        break;
      case "offer":
        if (!this.isCreator) await this.acceptOffer(msg.sdp);
        break;
      case "answer":
        if (this.isCreator && this.pc) await this.pc.setRemoteDescription({ type: "answer", sdp: msg.sdp });
        break;
      case "error":
        console.error("signaling server:", msg.reason);
        break;
    }
  }

  // ── WebRTC ────────────────────────────────────────────────────────

  private connected(): boolean {
    return this.ready !== undefined && !this.ready.isClosed;
  }

  /** Replace the current connection with a fresh one. */
  private newPeer(): RTCPeerConnection {
    this.pc?.close();
    this.ready = undefined;
    this.lastIceFailed = false;
    this.negotiationStarted = Date.now();
    this.onPhase("connecting");
    const pc = new RTCPeerConnection({ iceServers: this.iceServers });
    this.pc = pc;
    this.pulse();
    return pc;
  }

  private async negotiate(): Promise<void> {
    if (this.closing || this.ws?.readyState !== WebSocket.OPEN) return; // retried on next hello
    const pc = this.newPeer();
    this.watch(pc, pc.createDataChannel("whoosh"));
    await this.sendDescription(pc, await pc.createOffer());
  }

  private renegotiate(): void {
    if (!this.closing && !this.connected()) void this.negotiate();
  }

  private async acceptOffer(sdp: string | undefined): Promise<void> {
    const pc = this.newPeer();
    pc.addEventListener("datachannel", (event) => this.watch(pc, event.channel));
    await pc.setRemoteDescription({ type: "offer", sdp });
    await this.sendDescription(pc, await pc.createAnswer());
  }

  private async sendDescription(
    pc: RTCPeerConnection,
    description: RTCSessionDescriptionInit,
  ): Promise<void> {
    await pc.setLocalDescription(description);
    await iceGatheringComplete(pc);
    if (pc !== this.pc) return; // superseded while gathering
    const local = pc.localDescription!;
    try {
      this.sendSignal({ type: local.type, sdp: local.sdp });
    } catch {
      // signaling is down; the creator re-offers when it's back
    }
  }

  /** Track one connection: publish its channel when open, notice when it ends. */
  private watch(pc: RTCPeerConnection, dc: RTCDataChannel): void {
    const timer = setTimeout(() => {
      if (pc !== this.pc || this.connected()) return;
      this.lastIceFailed = pc.iceConnectionState === "failed";
      pc.close();
      this.pulse();
      if (this.isCreator && !this.closing) setTimeout(() => this.renegotiate(), 2000);
    }, CONNECT_TIMEOUT_MS);

    const open = () => {
      clearTimeout(timer);
      if (pc !== this.pc) {
        pc.close(); // superseded while connecting
        return;
      }
      const channel = new PeerChannel(dc, pc);
      this.ready = channel;
      this.pulse();
      channel.onClose(() => {
        if (this.ready === channel) {
          this.ready = undefined;
          this.pulse();
        }
      });
    };
    if (dc.readyState === "open") open();
    else dc.addEventListener("open", open);
  }
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
