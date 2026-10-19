/**
 * File transfer over a DataChannel, both directions — the browser port of
 * packages/transfer/manager.py. Same wire sequence:
 *
 *   file_metadata → binary chunks → transfer_complete(sha256)
 *   ← transfer_complete (receiver confirms after verifying)
 *
 * Either side may send transfer_cancel at any point.
 *
 * Resume (docs/decisions.md D46–D47): when the peer advertised `resume`,
 * file_metadata carries a resume_key and the receiver answers with
 * transfer_accept {offset}; the sender streams only the rest. Received
 * partials are kept in memory for the life of the tab (resume.ts).
 *
 * Acknowledgements (D49): with `ack`, the receiver sends transfer_ack every
 * ACK_EVERY bytes; the sender's progress counts acknowledged bytes and at
 * most ACK_WINDOW may be unacknowledged. A full window with no ack for the
 * stall timeout means the connection is dead even if it hasn't said so.
 */
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

import { safeFilename } from "./filenames";
import { CHUNK_SIZE, decodeFrame, encodeChunk, newTransferId, uuidHex } from "./frames";
import { type Hasher, type Partial, isResumeKey, resumeKey, tabPartials } from "./resume";
import {
  type FileMetadataPayload,
  type MessageMap,
  type MessageType,
  type ProtocolMessage,
  type TransferCompletePayload,
  encodeMessage,
} from "./protocol";

/** Wait this long for the receiver to confirm a fully sent file. */
const CONFIRM_TIMEOUT_MS = 60_000;
/** Wait this long for transfer_accept (the receiver may re-hash a partial). */
const ACCEPT_TIMEOUT_MS = 60_000;
/** Receivers acknowledge after storing this many more bytes. */
export const ACK_EVERY = 1024 * 1024;
/** Senders keep at most this many bytes unacknowledged. */
export const ACK_WINDOW = 8 * 1024 * 1024;
const STALL_TIMEOUT_MS = 20_000;
/** Minimum ms between progress callbacks for one transfer. */
const REPORT_INTERVAL_MS = 100;
/** Files are read in blocks this size, then cut into chunks. */
const READ_BLOCK = 1024 * 1024;
/** Received chunks are folded into a Blob every this many bytes, so the
 * browser can page them out of memory instead of holding one huge array. */
const BLOB_FLUSH_BYTES = 8 * 1024 * 1024;

export interface Channel {
  send(data: string | Uint8Array): void;
  /** Resolves when it's OK to send more; rejects with ConnectionClosedError. */
  drain(): Promise<void>;
}

export class ConnectionClosedError extends Error {
  constructor() {
    super("connection closed");
  }
}

export type Direction = "send" | "receive";
export type TransferStatus = "streaming" | "completed" | "cancelled" | "failed";

/** Immutable snapshot handed to the UI on every update. */
export interface Transfer {
  readonly id: string;
  readonly direction: Direction;
  readonly filename: string;
  readonly size: number;
  readonly bytesDone: number;
  readonly status: TransferStatus;
  readonly reason: string;
  readonly startedAt: number;
  /** Bytes already in place when this transfer started (resume). */
  readonly resumedFrom: number;
  /** Sender with acks: bytes the receiver confirmed storing. */
  readonly bytesAcked: number;
  /** Whether the receiver acknowledges (sender side). */
  readonly acked: boolean;
  /** Failed because the connection was lost or stalled, not because of the
   * data: worth retrying once reconnected (resume makes it cheap). */
  readonly interrupted: boolean;
  /** Object URL of the received file, once completed. */
  readonly url?: string;
}

class TransferState {
  bytesDone = 0;
  resumedFrom = 0;
  bytesAcked = 0;
  acked = false;
  interrupted = false;
  /** Receiver: send transfer_ack? And bytes covered by the last one sent. */
  sendAcks = false;
  lastAckSent = 0;
  /** Sender: woken whenever an ack arrives. */
  ackWaiters: (() => void)[] = [];
  status: TransferStatus = "streaming";
  reason = "";
  url?: string;
  readonly startedAt = performance.now();
  hash: Hasher = sha256.create();
  lastReport = 0;
  /** Receive side: resume key this partial is kept under, if resumable. */
  resumeKey?: string;
  // sender: resolved with the receiver's starting offset
  readonly accepted: Promise<number>;
  resolveAccepted!: (offset: number) => void;
  // receive side
  parts: Uint8Array[] = [];
  partBytes = 0;
  blobs: Blob[] = [];
  // resolved when the transfer finishes, for whatever reason
  readonly done: Promise<void>;
  resolveDone!: () => void;

  constructor(
    readonly id: string,
    readonly direction: Direction,
    readonly filename: string,
    readonly size: number,
    readonly mimeType: string,
  ) {
    this.done = new Promise((resolve) => (this.resolveDone = resolve));
    this.accepted = new Promise((resolve) => (this.resolveAccepted = resolve));
  }

  get finished(): boolean {
    return this.status !== "streaming";
  }

  snapshot(): Transfer {
    const { id, direction, filename, size, bytesDone, status, reason, startedAt, url } = this;
    const { resumedFrom, bytesAcked, acked, interrupted } = this;
    return {
      id,
      direction,
      filename,
      size,
      bytesDone,
      status,
      reason,
      startedAt,
      url,
      resumedFrom,
      bytesAcked,
      acked,
      interrupted,
    };
  }
}

export interface TransferOptions {
  confirmTimeoutMs?: number;
  acceptTimeoutMs?: number;
  stallTimeoutMs?: number;
  /** Minimum ms between progress callbacks per transfer. */
  reportIntervalMs?: number;
  /** Where interrupted receives are kept: the tab's store unless injected. */
  partials?: Map<string, Partial>;
  /** A transfer stalled: the connection is dead even if it hasn't said so. */
  onStall?: () => void;
}

export class TransferManager {
  private readonly transfers = new Map<string, TransferState>();
  private readonly confirmTimeoutMs: number;
  private readonly acceptTimeoutMs: number;
  private readonly stallTimeoutMs: number;
  private readonly reportIntervalMs: number;
  private readonly partials: Map<string, Partial>;
  private readonly onStall?: () => void;

  constructor(
    private channel: Channel,
    private readonly onUpdate: (transfer: Transfer) => void = () => {},
    options: TransferOptions = {},
  ) {
    this.confirmTimeoutMs = options.confirmTimeoutMs ?? CONFIRM_TIMEOUT_MS;
    this.acceptTimeoutMs = options.acceptTimeoutMs ?? ACCEPT_TIMEOUT_MS;
    this.stallTimeoutMs = options.stallTimeoutMs ?? STALL_TIMEOUT_MS;
    this.reportIntervalMs = options.reportIntervalMs ?? REPORT_INTERVAL_MS;
    this.partials = options.partials ?? tabPartials;
    this.onStall = options.onStall;
  }

  // ── Sending ───────────────────────────────────────────────────────

  /**
   * Stream `file` to the peer and wait for its confirmation. `resume`: the
   * peer advertised the resume capability, so offer a resume key and start
   * wherever the receiver says. `ack`: the peer advertised acks, so ask for
   * them, show acknowledged progress and keep a bounded window in flight.
   */
  async sendFile(file: File, { resume = false, ack = false } = {}): Promise<Transfer> {
    const st = new TransferState(
      newTransferId(),
      "send",
      file.name,
      file.size,
      file.type || "application/octet-stream",
    );
    st.acked = ack;
    this.transfers.set(st.id, st);

    try {
      this.send("file_metadata", {
        transfer_id: st.id,
        filename: st.filename,
        size: st.size,
        mime_type: st.mimeType,
        resume_key: resume ? await resumeKey(file) : null,
        ack,
      });
      const offset = resume ? await this.awaitAccept(st) : 0;
      if (st.finished) return st.snapshot();
      if (offset) await this.skipPrefix(st, file, offset);
      this.report(st, true);
      await this.stream(st, file, offset);

      if (!st.finished) {
        this.send("transfer_complete", { transfer_id: st.id, sha256: bytesToHex(st.hash.digest()) });
        if (!(await settlesWithin(st.done, this.confirmTimeoutMs))) {
          this.fail(st, "receiver did not confirm the file");
        }
      }
    } catch (err) {
      if (err instanceof ConnectionClosedError) {
        this.interrupt(st, "connection lost");
      } else {
        this.fail(st, `read error: ${(err as Error).message}`);
      }
    }
    return st.snapshot();
  }

  /** Wait for transfer_accept (or for the transfer to end first). */
  private async awaitAccept(st: TransferState): Promise<number> {
    const result = await Promise.race([
      st.accepted,
      st.done.then(() => undefined),
      new Promise<null>((resolve) => setTimeout(() => resolve(null), this.acceptTimeoutMs)),
    ]);
    if (st.finished || result === undefined) return 0;
    if (result === null) {
      this.fail(st, "receiver did not answer the transfer offer");
      return 0;
    }
    if (!Number.isSafeInteger(result) || result < 0 || result > st.size) {
      this.fail(st, `receiver asked for an invalid offset ${result}`);
      return 0;
    }
    return result;
  }

  /** Hash the part the receiver already has without sending it: the final
   * sha256 covers the whole file. */
  private async skipPrefix(st: TransferState, file: File, offset: number): Promise<void> {
    for (let pos = 0; pos < offset; pos += READ_BLOCK) {
      const end = Math.min(pos + READ_BLOCK, offset);
      st.hash.update(new Uint8Array(await file.slice(pos, end).arrayBuffer()));
    }
    st.bytesDone = st.resumedFrom = st.bytesAcked = offset;
  }

  private async stream(st: TransferState, file: File, offset: number): Promise<void> {
    while (offset < st.size) {
      const block = new Uint8Array(await file.slice(offset, offset + READ_BLOCK).arrayBuffer());
      if (block.length === 0) throw new Error("file shrank while being sent");
      for (let i = 0; i < block.length; i += CHUNK_SIZE) {
        if (st.acked && !(await this.windowOpen(st))) return;
        await this.channel.drain();
        // Either side may have cancelled while we waited for the buffer
        if (st.finished) return;
        const chunk = block.subarray(i, i + CHUNK_SIZE);
        this.channel.send(encodeChunk(st.id, offset, chunk));
        st.hash.update(chunk);
        offset += chunk.length;
        st.bytesDone = offset;
        this.report(st);
      }
    }
  }

  /** Wait until fewer than ACK_WINDOW bytes are unacknowledged. False if the
   * transfer ended meanwhile or acks stopped coming. */
  private async windowOpen(st: TransferState): Promise<boolean> {
    while (st.bytesDone - st.bytesAcked >= ACK_WINDOW) {
      if (st.finished) return false;
      const outcome = await new Promise<"ack" | "timeout">((resolve) => {
        const timer = setTimeout(() => resolve("timeout"), this.stallTimeoutMs);
        st.ackWaiters.push(() => {
          clearTimeout(timer);
          resolve("ack");
        });
      });
      if (outcome === "timeout") {
        // Nothing to tell the receiver (the path to it is what failed), and it
        // must keep its partial for the retry
        this.interrupt(st, `stalled: nothing acknowledged for ${this.stallTimeoutMs / 1000}s`);
        this.onStall?.();
        return false;
      }
    }
    return !st.finished;
  }

  // ── Receiving ─────────────────────────────────────────────────────

  /** Handle a transfer-related message; false if `msg` isn't one. */
  handleMessage(msg: ProtocolMessage): boolean {
    switch (msg.type) {
      case "file_metadata":
        this.startReceive(msg.payload);
        return true;
      case "transfer_ack": {
        const st = this.active(msg.payload.transfer_id);
        const offset = msg.payload.offset;
        if (st?.direction === "send" && st.bytesAcked <= offset && offset <= st.bytesDone) {
          st.bytesAcked = offset;
          this.wakeAckWaiters(st);
          this.report(st);
        }
        return true;
      }
      case "transfer_accept": {
        const st = this.active(msg.payload.transfer_id);
        if (st?.direction === "send") st.resolveAccepted(msg.payload.offset);
        return true;
      }
      case "transfer_complete":
        this.handleComplete(msg.payload);
        return true;
      case "transfer_cancel": {
        const st = this.active(msg.payload.transfer_id);
        if (st) this.finish(st, "cancelled", msg.payload.reason || "cancelled by peer", false);
        return true;
      }
      default:
        return false;
    }
  }

  /** Handle a binary frame. Throws FrameDecodeError if malformed. */
  handleFrame(buffer: ArrayBuffer): void {
    const chunk = decodeFrame(buffer);
    const st = this.active(chunk.transferId);
    if (!st || st.direction !== "receive") return; // e.g. in flight after a cancel

    if (chunk.offset !== st.bytesDone) {
      this.fail(st, `expected offset ${st.bytesDone}, got ${chunk.offset}`);
      return;
    }
    if (st.bytesDone + chunk.data.length > st.size) {
      this.fail(st, "received more data than announced");
      return;
    }

    st.parts.push(chunk.data);
    st.partBytes += chunk.data.length;
    if (st.partBytes >= BLOB_FLUSH_BYTES) {
      st.blobs.push(new Blob(st.parts as BlobPart[]));
      st.parts = [];
      st.partBytes = 0;
    }
    st.hash.update(chunk.data);
    st.bytesDone += chunk.data.length;
    if (st.sendAcks && st.bytesDone - st.lastAckSent >= ACK_EVERY) {
      st.lastAckSent = st.bytesDone;
      this.send("transfer_ack", { transfer_id: st.id, offset: st.bytesDone });
    }
    this.report(st);
  }

  private startReceive(meta: FileMetadataPayload): void {
    let id: string;
    try {
      id = uuidHex(meta.transfer_id);
    } catch {
      this.reject(meta.transfer_id, "invalid transfer id");
      return;
    }
    if (this.transfers.has(id)) return this.reject(meta.transfer_id, "duplicate transfer id");
    if (meta.size < 0) return this.reject(meta.transfer_id, "invalid size");
    const key = meta.resume_key ?? undefined;
    if (key !== undefined && !isResumeKey(key)) {
      return this.reject(meta.transfer_id, "invalid resume key");
    }

    const st = new TransferState(
      id,
      "receive",
      safeFilename(meta.filename),
      meta.size,
      meta.mime_type ?? "application/octet-stream",
    );
    // Resume under the sender's key, unless another active transfer has it
    const inUse = [...this.transfers.values()].some((t) => !t.finished && t.resumeKey === key);
    if (key !== undefined && !inUse) {
      st.resumeKey = key;
      const partial = this.partials.get(key);
      this.partials.delete(key); // owned by this transfer now
      if (partial && partial.size === meta.size && partial.bytes <= meta.size) {
        st.blobs = partial.blobs;
        st.hash = partial.hash;
        st.bytesDone = st.resumedFrom = partial.bytes;
      }
    }
    st.sendAcks = meta.ack ?? false;
    st.lastAckSent = st.bytesDone;
    this.transfers.set(id, st);
    if (key !== undefined) {
      // The sender offered resume, so it waits for our answer
      this.send("transfer_accept", { transfer_id: meta.transfer_id, offset: st.bytesDone });
    }
    this.report(st, true);
  }

  private handleComplete(payload: TransferCompletePayload): void {
    const st = this.active(payload.transfer_id);
    if (!st) return;

    if (st.direction === "send") {
      // The receiver's confirmation: verified and saved
      st.bytesAcked = st.size;
      this.finish(st, "completed", "", false);
      return;
    }
    if (st.bytesDone !== st.size) {
      this.fail(st, `incomplete: got ${st.bytesDone} of ${st.size} bytes`);
      return;
    }
    if (bytesToHex(st.hash.digest()) !== payload.sha256.toLowerCase()) {
      this.fail(st, "checksum mismatch");
      return;
    }

    const blob = new Blob([...st.blobs, ...(st.parts as BlobPart[])], { type: st.mimeType });
    st.blobs = [];
    st.parts = [];
    st.url = URL.createObjectURL(blob);
    this.send("transfer_complete", payload);
    this.finish(st, "completed", "", false);
  }

  // ── Cancel / teardown ─────────────────────────────────────────────

  cancel(id: string, reason = "cancelled by user"): boolean {
    const st = this.active(id);
    if (!st) return false;
    this.finish(st, "cancelled", reason, true);
    return true;
  }

  /** Fail every active transfer without notifying the peer (channel gone).
   * Resumable partials are kept. */
  close(reason = "connection closed"): void {
    for (const st of this.transfers.values()) {
      if (!st.finished) this.interrupt(st, reason);
    }
  }

  /** Release received files' object URLs. Call when leaving the room. */
  dispose(): void {
    this.close("left the room");
    for (const st of this.transfers.values()) {
      if (st.url) URL.revokeObjectURL(st.url);
    }
  }

  // ── Internals ─────────────────────────────────────────────────────

  private active(id: string): TransferState | undefined {
    let key = id;
    try {
      key = uuidHex(id);
    } catch {
      // not a UUID: can't match anything
    }
    const st = this.transfers.get(key);
    return st && !st.finished ? st : undefined;
  }

  private wakeAckWaiters(st: TransferState): void {
    const waiters = st.ackWaiters;
    st.ackWaiters = [];
    for (const wake of waiters) wake();
  }

  /** Use a new channel after a reconnect. Transfers on the old one have
   * already ended (close()); they continue by being sent again. */
  attach(channel: Channel): void {
    this.channel = channel;
  }

  /** End a transfer because the connection failed: keep partial data and
   * flag it for a retry. */
  private interrupt(st: TransferState, reason: string): void {
    if (st.finished) return;
    st.interrupted = true;
    this.finish(st, "failed", reason, false, true);
  }

  private fail(st: TransferState, reason: string): void {
    this.finish(st, "failed", reason, true);
  }

  /** End a transfer. Received data is dropped unless `keepPartial` and it's
   * resumable: cancels and integrity failures must leave nothing to resume. */
  private finish(
    st: TransferState,
    status: TransferStatus,
    reason: string,
    notify: boolean,
    keepPartial = false,
  ): void {
    if (st.finished) return;
    st.status = status;
    st.reason = reason;
    if (status !== "completed") {
      if (keepPartial && st.resumeKey !== undefined && st.bytesDone > 0) {
        this.partials.set(st.resumeKey, {
          size: st.size,
          bytes: st.bytesDone,
          blobs: [...st.blobs, new Blob(st.parts as BlobPart[])],
          hash: st.hash.clone(),
        });
      }
      st.parts = [];
      st.blobs = [];
    }
    if (notify) {
      try {
        this.send("transfer_cancel", { transfer_id: st.id, reason });
      } catch {
        // channel already gone; nothing to tell
      }
    }
    this.wakeAckWaiters(st); // a sender waiting on the window sees the end
    st.resolveDone();
    this.report(st, true);
  }

  private reject(transferId: string, reason: string): void {
    console.warn(`rejecting incoming transfer ${transferId}: ${reason}`);
    try {
      this.send("transfer_cancel", { transfer_id: transferId, reason });
    } catch {
      // channel already gone
    }
  }

  private send<K extends MessageType>(type: K, payload: MessageMap[K]): void {
    this.channel.send(encodeMessage(type, payload));
  }

  private report(st: TransferState, force = false): void {
    const now = performance.now();
    if (force || now - st.lastReport >= this.reportIntervalMs) {
      st.lastReport = now;
      this.onUpdate(st.snapshot());
    }
  }
}

/** Whether `promise` settles within `ms`. Clears its timer either way. */
function settlesWithin(promise: Promise<unknown>, ms: number): Promise<boolean> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), ms);
    void promise.then(() => {
      clearTimeout(timer);
      resolve(true);
    });
  });
}
