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
import { type Transfer, TransferManager } from "./transfer";

export const CAPABILITIES: Capability[] = ["chat", "file_transfer", "cancel", "resume"];

export interface SessionEvents {
  onChat(text: string): void;
  onNotice(text: string): void;
  onTransfer(transfer: Transfer): void;
  onPeerReady(capabilities: Capability[]): void;
  /** Round-trip time from a ping this side sent. */
  onLatency(ms: number): void;
  onClosed(): void;
}

export class Session {
  readonly transfers: TransferManager;
  private peerCapabilities: Set<Capability> | undefined;

  constructor(
    private readonly channel: PeerChannel,
    private readonly events: SessionEvents,
  ) {
    this.transfers = new TransferManager(channel, events.onTransfer);

    // DataChannel events are delivered one at a time, in order, and every
    // handler below is synchronous, so messages are processed in order
    // (the property packages/rtc needed a queue for — docs/decisions.md D24).
    channel.dc.addEventListener("message", (event: MessageEvent<string | ArrayBuffer>) =>
      this.onMessage(event.data),
    );
    channel.onClose(() => {
      this.transfers.close("connection closed");
      events.onClosed();
    });
    this.send("hello", { capabilities: CAPABILITIES });
  }

  canSendFiles(): boolean {
    return this.peerSupports("file_transfer");
  }

  peerSupports(capability: Capability): boolean {
    return this.peerCapabilities?.has(capability) ?? false;
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
    this.channel.close();
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
          this.events.onPeerReady(msg.payload.capabilities);
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
      case "error":
        // Never answer an error with an error — that's how loops start
        this.events.onNotice(`Peer reported an error: ${msg.payload.message}`);
        break;
    }
  }

  private send<K extends MessageType>(type: K, payload: MessageMap[K]): void {
    this.channel.send(encodeMessage(type, payload));
  }

  private sendError(code: string, message: string): void {
    console.warn(`bad input from peer (${code}): ${message}`);
    this.send("error", { code, message });
  }
}
