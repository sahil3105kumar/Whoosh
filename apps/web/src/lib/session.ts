/**
 * Application protocol over an open DataChannel: hello, chat, ping and
 * file transfer. Browser counterpart of apps/cli/session.py.
 */
import type { PeerChannel } from "./connection";
import { FrameDecodeError } from "./frames";
import {
  type Capability,
  type MessageMap,
  type MessageType,
  ProtocolError,
  type ProtocolMessage,
  decodeMessage,
  encodeMessage,
} from "./protocol";
import { type Channel, ConnectionClosedError, type Transfer, TransferManager } from "./transfer";

export const CAPABILITIES: Capability[] = ["chat", "file_transfer", "cancel", "resume", "ack"];

export interface SessionEvents {
  onChat(text: string): void;
  onNotice(text: string): void;
  onTransfer(transfer: Transfer): void;
  onPeerReady(capabilities: Capability[]): void;
  /** Round-trip time from a ping this side sent. */
  onLatency(ms: number): void;
  onClosed(): void;
}

/** Stands in before the first connection: every send fails as closed. */
const notConnected: Channel = {
  send() {
    throw new ConnectionClosedError();
  },
  async drain() {
    throw new ConnectionClosedError();
  },
};

/**
 * The protocol conversation with the other peer. Survives reconnects:
 * attach() moves it onto each new connection (docs/decisions.md D50).
 */
export class Session {
  readonly transfers: TransferManager;
  private channel?: PeerChannel;
  private peerCapabilities: Set<Capability> | undefined;
  /** Bumped on every attach, so waiters can tell a new connection from the
   * one that just failed. */
  generation = 0;
  /** The other side said bye: it left on purpose, don't reconnect. */
  peerLeft = false;
  private readyWaiters: (() => void)[] = [];

  /** How the other peer is shown: its chosen name once its hello arrives. */
  peerName: string;

  constructor(
    private readonly events: SessionEvents,
    onStall?: () => void,
    /** Our display name, sent in hello. */
    private readonly name?: string,
    peerId = "",
  ) {
    this.peerName = peerId ? `peer-${peerId.slice(0, 4)}` : "peer";
    this.transfers = new TransferManager(notConnected, events.onTransfer, { onStall });
  }

  /** Continue on a (new) connection. */
  attach(channel: PeerChannel): void {
    this.channel = channel;
    this.peerCapabilities = undefined;
    this.generation++;
    this.transfers.attach(channel);

    // DataChannel events are delivered one at a time, in order, and every
    // handler below is synchronous, so messages are processed in order
    // (the property packages/rtc needed a queue for — docs/decisions.md D24).
    channel.dc.addEventListener("message", (event: MessageEvent<string | ArrayBuffer>) => {
      if (this.channel === channel) this.onMessage(event.data);
    });
    channel.onClose(() => {
      if (this.channel !== channel) return;
      this.transfers.close("connection closed");
      this.events.onClosed();
    });
    this.send("hello", { capabilities: CAPABILITIES, name: this.name || null });
  }

  get connected(): boolean {
    return this.channel !== undefined && !this.channel.isClosed;
  }

  /** Wait for a connection newer than generation `after` whose hello has
   * arrived; false on timeout. */
  async waitReady(after: number, timeoutMs: number): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    while (!(this.generation > after && this.connected && this.peerCapabilities)) {
      const remaining = deadline - Date.now();
      if (remaining <= 0) return false;
      await new Promise<void>((resolve) => {
        this.readyWaiters.push(resolve);
        setTimeout(resolve, Math.min(remaining, 500));
      });
    }
    return true;
  }

  /** Tell the other side we're leaving on purpose. */
  sayBye(): void {
    try {
      this.send("bye", {});
    } catch {
      // already disconnected
    }
  }

  canSendFiles(): boolean {
    return this.peerSupports("file_transfer");
  }

  peerSupports(capability: Capability): boolean {
    return this.peerCapabilities?.has(capability) ?? false;
  }

  /** sendFile options for what the peer supports. */
  sendOptions(): { resume: boolean; ack: boolean } {
    return { resume: this.peerSupports("resume"), ack: this.peerSupports("ack") };
  }

  sendChat(text: string): void {
    this.send("chat", { text });
  }

  ping(): void {
    // performance.now() is fine across machines: the peer only echoes it
    this.send("ping", { timestamp: performance.now() });
  }

  close(): void {
    this.transfers.dispose();
    this.channel?.close();
  }

  private onMessage(data: string | ArrayBuffer): void {
    if (typeof data !== "string") {
      try {
        this.transfers.handleFrame(data);
      } catch (err) {
        if (!(err instanceof FrameDecodeError)) throw err;
        this.sendError("bad_frame", err.message);
      }
      return;
    }

    let msg: ProtocolMessage;
    try {
      msg = decodeMessage(data);
    } catch (err) {
      if (!(err instanceof ProtocolError)) throw err;
      this.sendError("bad_message", err.message);
      return;
    }
    this.dispatch(msg);
  }

  private dispatch(msg: ProtocolMessage): void {
    if (this.transfers.handleMessage(msg)) return;
    switch (msg.type) {
      case "hello":
        if (!this.peerCapabilities) {
          this.peerCapabilities = new Set(msg.payload.capabilities);
          if (msg.payload.name) this.peerName = cleanName(msg.payload.name);
          this.events.onPeerReady(msg.payload.capabilities);
          for (const wake of this.readyWaiters.splice(0)) wake();
        }
        break;
      case "chat":
        this.events.onChat(msg.payload.text);
        break;
      case "ping":
        this.send("pong", { timestamp: msg.payload.timestamp });
        break;
      case "pong":
        this.events.onLatency(performance.now() - msg.payload.timestamp);
        break;
      case "bye":
        this.peerLeft = true;
        break;
      case "error":
        // Never answer an error with an error — that's how loops start
        this.events.onNotice(`Peer reported an error: ${msg.payload.message}`);
        break;
    }
  }

  private send<K extends MessageType>(type: K, payload: MessageMap[K]): void {
    if (!this.channel) throw new ConnectionClosedError();
    this.channel.send(encodeMessage(type, payload));
  }

  private sendError(code: string, message: string): void {
    console.warn(`bad input from peer (${code}): ${message}`);
    this.send("error", { code, message });
  }
}

/** A peer's self-chosen name, made safe to show: printable, at most 32 characters. */
export function cleanName(name: string): string {
  const printable = [...name].filter((c) => !/[\p{C}]/u.test(c)).join("").trim();
  return printable.slice(0, 32) || "peer";
}
