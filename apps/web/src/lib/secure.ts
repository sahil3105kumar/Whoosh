/**
 * An end-to-end encrypted view of a peer connection (D60) — the browser
 * side of packages/crypto/channel.py.
 *
 * hello and key_exchange are the only messages sent in the clear (still
 * inside DTLS); anything sent before the exchange completes is held back,
 * then sealed. WebCrypto is asynchronous, so sealing and opening each run
 * through a promise chain that keeps messages in order (frame counters,
 * and the transfer layer, depend on it — D24).
 */
import type { PeerChannel } from "./connection";
import {
  E2EError,
  type Established,
  type Identity,
  KIND_ENCRYPTED,
  KeyExchange,
  type KeyExchangeMessage,
  OVERHEAD,
} from "./e2e";
import { type Channel, ConnectionClosedError } from "./transfer";

export type MessageHandler = (data: string | ArrayBuffer) => void | Promise<void>;

export class SecureChannel implements Channel {
  established?: Established;
  /** Why the channel was given up, if it was. */
  failure?: string;
  private held: (string | Uint8Array)[] = [];
  private sendChain: Promise<void> = Promise.resolve();
  private receiveChain: Promise<void> = Promise.resolve();
  private readonly settled: Promise<void>;
  private resolveSettled!: () => void;

  private constructor(
    readonly inner: PeerChannel,
    private readonly exchange: KeyExchange,
  ) {
    this.settled = new Promise((resolve) => (this.resolveSettled = resolve));
  }

  static async create(inner: PeerChannel, identity: Identity): Promise<SecureChannel> {
    return new SecureChannel(inner, await KeyExchange.create(identity));
  }

  get maxMessageSize(): number {
    return this.inner.maxMessageSize - OVERHEAD;
  }

  get isClosed(): boolean {
    return this.inner.isClosed || this.failure !== undefined;
  }

  /** Called once when the connection ends — after every message that
   * arrived before it has been decrypted and handled. Otherwise a "bye"
   * still being decrypted would be missed, and a deliberate exit would look
   * like a dropped connection. */
  onClose(listener: () => void): void {
    this.inner.onClose(() => {
      this.receiveChain = this.receiveChain.then(listener, listener);
    });
  }

  close(): void {
    this.inner.close();
  }

  /** Send end-to-end encrypted, or hold until the exchange completes. */
  send(data: string | Uint8Array): void {
    if (this.failure !== undefined || this.inner.isClosed) throw new ConnectionClosedError();
    if (!this.established) {
      this.held.push(data);
      return;
    }
    this.seal(data);
  }

  /** For hello, key_exchange and bye only. */
  sendPlain(text: string): void {
    this.inner.send(text);
  }

  async drain(): Promise<void> {
    await this.settled;
    if (this.failure !== undefined) throw new ConnectionClosedError();
    await this.sendChain;
    await this.inner.drain();
  }

  /** Deliver incoming messages to `handler`, decrypted, strictly in order. */
  onMessage(handler: MessageHandler): void {
    this.inner.dc.addEventListener("message", (event: MessageEvent<string | ArrayBuffer>) => {
      const raw = event.data;
      this.receiveChain = this.receiveChain
        .then(async () => {
          if (this.failure !== undefined) return;
          if (!this.established) return handler(raw); // hello / key_exchange
          if (typeof raw !== "string" && new Uint8Array(raw)[0] === KIND_ENCRYPTED) {
            const data = await this.established.sealer.open(new Uint8Array(raw));
            return handler(typeof data === "string" ? data : data.buffer);
          }
          // bye stays in the clear so it can go out synchronously at the last
          // moment (a closing tab can't await encryption); it carries nothing
          if (typeof raw === "string" && isBye(raw)) return handler(raw);
          throw new E2EError("the peer sent an unencrypted message after the key exchange");
        })
        .catch((err: unknown) => this.fail((err as Error).message));
    });
  }

  keyExchangeMessage(): Promise<KeyExchangeMessage> {
    return this.exchange.message();
  }

  /** Finish the exchange and release held messages. Throws E2EError. */
  async complete(peer: KeyExchangeMessage): Promise<Established> {
    if (this.established) throw new E2EError("a second key exchange on the same connection");
    this.established = await this.exchange.complete(peer);
    for (const data of this.held.splice(0)) this.seal(data);
    this.resolveSettled();
    return this.established;
  }

  /** Give up on this connection: it can't be trusted. */
  fail(reason: string): void {
    if (this.failure !== undefined) return;
    console.warn("end-to-end encryption failed:", reason);
    this.failure = reason;
    this.held = [];
    this.resolveSettled();
    this.inner.close();
  }

  private seal(data: string | Uint8Array): void {
    const sealer = this.established!.sealer;
    // Counters are assigned when seal() runs, so it must run in order
    this.sendChain = this.sendChain
      .then(async () => this.inner.send(await sealer.seal(data)))
      .catch(() => {
        // the connection closed under us; its close handler reports it
      });
  }
}

function isBye(text: string): boolean {
  try {
    return (JSON.parse(text) as { type?: unknown }).type === "bye";
  } catch {
    return false;
  }
}
