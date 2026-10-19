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

  /** Largest message both ends accept (Chrome↔Chrome 256 KiB, with aiortc 64 KiB). */
  get maxMessageSize(): number {
    return this.pc.sctp?.maxMessageSize ?? 65536;
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

/** WebSocket close code the server uses for "room not found / expired / full". */
const POLICY_VIOLATION = 1008;

/** Turn the server's WebSocket close reason into advice for a person. */
export function explainClose(reason: string, roomId: string): string {
  if (reason.startsWith("No such room")) {
    return `Room ${roomId} doesn't exist. Check the code; rooms also expire 10 minutes after they're created.`;
  }
  if (reason.startsWith("Room expired")) return `Room ${roomId} has expired. Ask for a new code.`;
  if (reason.startsWith("Room is full")) return `Room ${roomId} is full.`;
  return reason;
}

interface Signal {
  type?: string;
  sdp?: string;
  reason?: string;
  peer_id?: string;
  peers?: string[];
  from?: string;
}

export type MeshEvent =
  /** An open connection to a peer: new, or replacing one that dropped. */
  | { type: "connected"; peerId: string; channel: PeerChannel }
  /** The peer left the room for good (its slot was released). */
  | { type: "gone"; peerId: string }
  /** Connecting to the peer timed out; the offering side keeps retrying. */
  | { type: "unreachable"; peerId: string; message: string }
  /** The room can't be used any more (gone, full, server unreachable). */
  | { type: "fatal"; message: string };

/** Our connection to one other peer. */
interface Pair {
  peerId: string;
  pc?: RTCPeerConnection;
  ready?: PeerChannel;
  retry?: ReturnType<typeof setTimeout>;
}

/**
 * Signaling + WebRTC for one room with any number of peers (#23) — the
 * browser side of apps/cli/connection.py.
 *
 * Owns the signaling WebSocket and a connection to every other peer (a full
 * mesh, docs/decisions.md D55). Each pair reconnects on its own when its
 * connection drops (D50); in every pair the smaller peer_id makes the
 * offers. A dropped WebSocket reconnects with its peer_id to keep its slot.
 *
 * SDP is sent once ICE gathering completes, with candidates inline — no
 * trickle ICE, because the Python client (aiortc) neither sends nor accepts
 * trickled candidates (D35).
 */
export class Mesh {
  peerId?: string;
  private iceServers: RTCIceServer[] = [];
  private ws?: WebSocket;
  private pairs = new Map<string, Pair>();
  private forgotten = new Set<string>();
  private closing = false;
  private backoffMs = 500;

  constructor(
    readonly roomId: string,
    private readonly onEvent: (event: MeshEvent) => void,
    private readonly onPhase: (phase: ConnectPhase) => void = () => {},
  ) {}

  async start(signal: AbortSignal): Promise<void> {
    this.iceServers = await fetchIceServers();
    // Bail before opening the socket: a cancelled attempt that reached the
    // server would hold a room slot (React StrictMode runs effects twice).
    if (signal.aborted) throw new ConnectError("Cancelled.");
    signal.addEventListener("abort", () => this.close());
    this.onPhase("signaling");
    this.connectSignaling();
  }

  /** Abandon the connection to `peerId` (it stalled), so a new one is made. */
  drop(peerId: string): void {
    const pair = this.pairs.get(peerId);
    pair?.ready?.close();
    pair?.pc?.close();
  }

  /** `peerId` left on purpose: stop trying to reach it. */
  forget(peerId: string): void {
    this.forgotten.add(peerId);
    const pair = this.pairs.get(peerId);
    this.pairs.delete(peerId);
    if (pair) {
      clearTimeout(pair.retry);
      pair.pc?.close();
    }
  }

  close(): void {
    this.closing = true;
    for (const pair of this.pairs.values()) {
      clearTimeout(pair.retry);
      pair.ready?.close();
      pair.pc?.close();
    }
    this.ws?.close();
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
      this.onSignal(msg).catch((err: unknown) => console.error("WebRTC negotiation failed", err));
    });
    ws.addEventListener("close", (event) => {
      if (this.ws === ws) this.ws = undefined;
      if (this.closing) return;
      if (event.code === POLICY_VIOLATION) {
        // Room gone, expired or full: reconnecting can't help. Live P2P
        // connections are unaffected; they just can't be re-made.
        this.onEvent({ type: "fatal", message: explainClose(event.reason, this.roomId) });
        return;
      }
      if (this.peerId === undefined) {
        this.onEvent({ type: "fatal", message: "Can't reach the signaling server." });
        return;
      }
      setTimeout(() => this.connectSignaling(), this.backoffMs);
      this.backoffMs = Math.min(this.backoffMs * 2, 10_000);
    });
  }

  private sendSignal(to: string, msg: object): void {
    if (this.ws?.readyState !== WebSocket.OPEN) throw new Error("signaling is down");
    this.ws.send(JSON.stringify({ ...msg, to }));
  }

  private async onSignal(msg: Signal): Promise<void> {
    switch (msg.type) {
      case "hello":
        this.peerId = msg.peer_id;
        this.backoffMs = 500;
        if (this.pairs.size === 0 && !msg.peers?.length) this.onPhase("waiting");
        for (const other of msg.peers ?? []) this.learn(other);
        // Back on signaling: retry every pair that's down
        for (const pair of this.pairs.values()) if (!connected(pair)) this.maybeOffer(pair);
        break;
      case "peer_joined":
        if (msg.peer_id) this.learn(msg.peer_id);
        break;
      case "peer_left":
        if (msg.peer_id && this.pairs.delete(msg.peer_id)) {
          this.onEvent({ type: "gone", peerId: msg.peer_id });
        }
        break;
      case "offer":
        if (msg.from) await this.acceptOffer(msg.from, msg.sdp);
        break;
      case "answer": {
        const pc = msg.from ? this.pairs.get(msg.from)?.pc : undefined;
        if (pc) await pc.setRemoteDescription({ type: "answer", sdp: msg.sdp });
        break;
      }
      case "error":
        console.debug("signaling server:", msg.reason);
        break;
    }
  }

  /** A peer is in the room: make sure we're connected to it. */
  private learn(peerId: string): void {
    if (peerId === this.peerId || this.forgotten.has(peerId)) return;
    let pair = this.pairs.get(peerId);
    if (!pair) {
      pair = { peerId };
      this.pairs.set(peerId, pair);
      this.onPhase("connecting");
    }
    if (!connected(pair)) this.maybeOffer(pair);
  }

  // ── WebRTC ────────────────────────────────────────────────────────

  /** In each pair the smaller peer_id offers (D55). */
  private offersTo(pair: Pair): boolean {
    return this.peerId !== undefined && this.peerId < pair.peerId;
  }

  private maybeOffer(pair: Pair, delayMs = 0): void {
    if (!this.offersTo(pair) || this.closing || pair.retry !== undefined) return;
    pair.retry = setTimeout(() => {
      pair.retry = undefined;
      void this.offer(pair);
    }, delayMs);
  }

  private async offer(pair: Pair): Promise<void> {
    if (this.closing || this.ws?.readyState !== WebSocket.OPEN || connected(pair)) return;
    if (this.pairs.get(pair.peerId) !== pair) return; // gone meanwhile
    const pc = this.newPeer(pair);
    this.watch(pair, pc, pc.createDataChannel("whoosh"));
    await this.sendDescription(pair, pc, await pc.createOffer());
  }

  /** Any offer from `sender` replaces our connection to it. */
  private async acceptOffer(sender: string, sdp: string | undefined): Promise<void> {
    if (this.forgotten.has(sender)) return;
    let pair = this.pairs.get(sender);
    if (!pair) {
      pair = { peerId: sender };
      this.pairs.set(sender, pair);
    }
    const pc = this.newPeer(pair);
    const target = pair;
    pc.addEventListener("datachannel", (event) => this.watch(target, pc, event.channel));
    await pc.setRemoteDescription({ type: "offer", sdp });
    await this.sendDescription(pair, pc, await pc.createAnswer());
  }

  private newPeer(pair: Pair): RTCPeerConnection {
    pair.pc?.close();
    pair.ready = undefined;
    const pc = new RTCPeerConnection({ iceServers: this.iceServers });
    pair.pc = pc;
    return pc;
  }

  private async sendDescription(
    pair: Pair,
    pc: RTCPeerConnection,
    description: RTCSessionDescriptionInit,
  ): Promise<void> {
    await pc.setLocalDescription(description);
    await iceGatheringComplete(pc);
    if (pc !== pair.pc) return; // superseded while gathering
    const local = pc.localDescription!;
    try {
      this.sendSignal(pair.peerId, { type: local.type, sdp: local.sdp });
    } catch {
      // signaling is down; the offering side retries when it's back
    }
  }

  /** Track one connection: publish its channel when open, notice when it ends. */
  private watch(pair: Pair, pc: RTCPeerConnection, dc: RTCDataChannel): void {
    const timer = setTimeout(() => {
      if (pc !== pair.pc || connected(pair)) return;
      const iceFailed = pc.iceConnectionState === "failed";
      pc.close();
      this.onEvent({
        type: "unreachable",
        peerId: pair.peerId,
        message: iceFailed ? directFailedMessage : "Timed out connecting.",
      });
      this.maybeOffer(pair, 2000); // keep trying
    }, CONNECT_TIMEOUT_MS);

    const open = () => {
      clearTimeout(timer);
      if (pc !== pair.pc || this.pairs.get(pair.peerId) !== pair) {
        pc.close(); // superseded while connecting
        return;
      }
      const channel = new PeerChannel(dc, pc);
      pair.ready = channel;
      this.onEvent({ type: "connected", peerId: pair.peerId, channel });
      channel.onClose(() => {
        if (pair.ready !== channel) return;
        pair.ready = undefined;
        // Dropped without a bye (the app forgets peers that say bye): the
        // offering side tries again shortly
        this.maybeOffer(pair, 1000);
      });
    };
    if (dc.readyState === "open") open();
    else dc.addEventListener("open", open);
  }
}

function connected(pair: Pair): boolean {
  return pair.ready !== undefined && !pair.ready.isClosed;
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
