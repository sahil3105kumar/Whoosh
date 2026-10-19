/**
 * Application protocol over an open DataChannel: hello, chat, ping and
 * file transfer. Browser counterpart of apps/cli/session.py.
 */
import type { PeerChannel } from "./connection";
import { E2EError, type Identity } from "./e2e";
import { FrameDecodeError } from "./frames";
import type { TrustStore } from "./identity";
import {
  type Capability,
  type MessageMap,
  type MessageType,
  ProtocolError,
  type ProtocolMessage,
  decodeMessage,
  encodeMessage,
} from "./protocol";
import { SecureChannel } from "./secure";
import { type Channel, ConnectionClosedError, type Transfer, TransferManager } from "./transfer";

export const CAPABILITIES: Capability[] = [
  "chat",
  "file_transfer",
  "cancel",
  "resume",
  "ack",
  "e2e",
];

/** What the user knows about who they're talking to (#29, #30, #32). */
export interface Security {
  /** The 6 emoji to compare, for the current connection. */
  sas: (readonly [string, string])[];
  fingerprint: string;
  verified: boolean;
  /** A device verified under this name before isn't the one connected now. */
  impostorWarning: boolean;
}

export interface SessionEvents {
  onChat(text: string): void;
  onNotice(text: string): void;
  onTransfer(transfer: Transfer): void;
  onPeerReady(capabilities: Capability[]): void;
  /** Round-trip time from a ping this side sent. */
  onLatency(ms: number): void;
  /** Encryption is up (or the peer's identity changed). */
  onSecurity(security: Security): void;
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
  private channel?: SecureChannel;
  /** The peer's capabilities from its hello, until encryption is up. */
  private pendingCapabilities?: Set<Capability>;
  /** Set once hello has arrived AND encryption is established: nothing
   * else may be sent until then. */
  private peerCapabilities: Set<Capability> | undefined;
  security?: Security;
  /** The peer couldn't be trusted (no encryption, bad keys): don't reconnect. */
  refused = false;
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
    onStall: (() => void) | undefined,
    /** Our display name, sent in hello. */
    private readonly name: string | undefined,
    peerId: string,
    private readonly identity: Identity,
    private readonly trust: TrustStore,
  ) {
    this.peerName = peerId ? `peer-${peerId.slice(0, 4)}` : "peer";
    this.transfers = new TransferManager(notConnected, events.onTransfer, { onStall });
  }

  /** Continue on a (new) connection, end-to-end encrypted (D60). */
  async attach(inner: PeerChannel): Promise<void> {
    const channel = await SecureChannel.create(inner, this.identity);
    this.channel = channel;
    this.pendingCapabilities = undefined;
    this.peerCapabilities = undefined;
    this.generation++;
    this.transfers.attach(channel);

    // Messages arrive (decrypted) one at a time, in order (D24)
    channel.onMessage((data) => {
      if (this.channel === channel) return this.onMessage(data);
    });
    channel.onClose(() => {
      if (this.channel !== channel) return;
      this.transfers.close("connection closed");
      this.events.onClosed();
    });
    channel.sendPlain(encodeMessage("hello", { capabilities: CAPABILITIES, name: this.name || null }));
  }

  /** The user compared codes and they matched (#32). */
  markVerified(): void {
    if (!this.security) return;
    this.trust.verify(this.security.fingerprint, this.peerName);
    this.security = { ...this.security, verified: true, impostorWarning: false };
    this.events.onSecurity(this.security);
  }

  /** Drop a peer that can't be trusted, and don't reconnect to it. */
  refuse(reason: string): void {
    this.events.onNotice(reason);
    this.refused = true;
    this.peerLeft = true;
    this.channel?.fail(reason);
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
    // In the clear, so it goes out synchronously (see SecureChannel)
    try {
      this.channel?.sendPlain(encodeMessage("bye", {}));
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

  private async onMessage(data: string | ArrayBuffer): Promise<void> {
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
    const allowedInClear = msg.type === "hello" || msg.type === "key_exchange" || msg.type === "bye";
    if (!this.channel?.established && !allowedInClear) {
      if (msg.type !== "error") this.refuse(`${this.peerName} sent ${msg.type} before encrypting`);
      return;
    }
    await this.dispatch(msg);
  }

  private async dispatch(msg: ProtocolMessage): Promise<void> {
    if (this.transfers.handleMessage(msg)) return;
    switch (msg.type) {
      case "hello": {
        if (msg.payload.name) this.peerName = cleanName(msg.payload.name);
        this.pendingCapabilities = new Set(msg.payload.capabilities);
        if (!this.pendingCapabilities.has("e2e")) {
          this.refuse(`${this.peerName} doesn't support end-to-end encryption; not connecting.`);
          return;
        }
        const kx = await this.channel!.keyExchangeMessage();
        this.channel!.sendPlain(encodeMessage("key_exchange", kx));
        break;
      }
      case "key_exchange":
        await this.keyExchange(msg.payload);
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

  private async keyExchange(payload: MessageMap["key_exchange"]): Promise<void> {
    let established;
    try {
      established = await this.channel!.complete(payload);
    } catch (err) {
      if (!(err instanceof E2EError)) throw err;
      this.refuse(`Couldn't set up encryption with ${this.peerName}: ${err.message}`);
      return;
    }
    const fingerprint = established.peerFingerprint;
    const changed = this.security !== undefined && this.security.fingerprint !== fingerprint;
    const verified = !changed && this.trust.isVerified(fingerprint);
    const impostorWarning =
      !verified && this.trust.verifiedAs(this.peerName).some((fp) => fp !== fingerprint);
    if (changed) {
      this.events.onNotice(
        `${this.peerName} reconnected from a DIFFERENT device. If you didn't expect that, ` +
          "someone may be intercepting: compare codes again.",
      );
    }
    this.security = { sas: established.sas, fingerprint, verified, impostorWarning };
    this.events.onSecurity(this.security);

    this.peerCapabilities = this.pendingCapabilities;
    this.events.onPeerReady([...(this.peerCapabilities ?? [])]);
    for (const wake of this.readyWaiters.splice(0)) wake();
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
