/**
 * File transfer over a DataChannel, both directions — the browser port of
 * packages/transfer/manager.py. Same wire sequence:
 *
 *   file_metadata → binary chunks → transfer_complete(sha256)
 *   ← transfer_complete (receiver confirms after verifying)
 *
 * Either side may send transfer_cancel at any point.
 */
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

import { safeFilename } from "./filenames";
import { CHUNK_SIZE, decodeFrame, encodeChunk, newTransferId, uuidHex } from "./frames";
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
  /** Object URL of the received file, once completed. */
  readonly url?: string;
}

class TransferState {
  bytesDone = 0;
  status: TransferStatus = "streaming";
  reason = "";
  url?: string;
  readonly startedAt = performance.now();
  readonly hash = sha256.create();
  lastReport = 0;
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
  }

  get finished(): boolean {
    return this.status !== "streaming";
  }

  snapshot(): Transfer {
    const { id, direction, filename, size, bytesDone, status, reason, startedAt, url } = this;
    return { id, direction, filename, size, bytesDone, status, reason, startedAt, url };
  }
}

export class TransferManager {
  private readonly transfers = new Map<string, TransferState>();

  constructor(
    private readonly channel: Channel,
    private readonly onUpdate: (transfer: Transfer) => void = () => {},
    private readonly confirmTimeoutMs = CONFIRM_TIMEOUT_MS,
  ) {}

  // ── Sending ───────────────────────────────────────────────────────

  /** Stream `file` to the peer and wait for its confirmation. */
  async sendFile(file: File): Promise<Transfer> {
    const st = new TransferState(
      newTransferId(),
      "send",
      file.name,
      file.size,
      file.type || "application/octet-stream",
    );
    this.transfers.set(st.id, st);

    try {
      this.send("file_metadata", {
        transfer_id: st.id,
        filename: st.filename,
        size: st.size,
        mime_type: st.mimeType,
      });
      this.report(st, true);
      await this.stream(st, file);

      if (!st.finished) {
        this.send("transfer_complete", { transfer_id: st.id, sha256: bytesToHex(st.hash.digest()) });
        if (!(await settlesWithin(st.done, this.confirmTimeoutMs))) {
          this.fail(st, "receiver did not confirm the file");
        }
      }
    } catch (err) {
      if (err instanceof ConnectionClosedError) {
        this.finish(st, "failed", "connection lost", false);
      } else {
        this.fail(st, `read error: ${(err as Error).message}`);
      }
    }
    return st.snapshot();
  }

  private async stream(st: TransferState, file: File): Promise<void> {
    let offset = 0;
    while (offset < st.size) {
      const block = new Uint8Array(await file.slice(offset, offset + READ_BLOCK).arrayBuffer());
      if (block.length === 0) throw new Error("file shrank while being sent");
      for (let i = 0; i < block.length; i += CHUNK_SIZE) {
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

  // ── Receiving ─────────────────────────────────────────────────────

  /** Handle a transfer-related message; false if `msg` isn't one. */
  handleMessage(msg: ProtocolMessage): boolean {
    switch (msg.type) {
      case "file_metadata":
        this.startReceive(msg.payload);
        return true;
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

    const st = new TransferState(
      id,
      "receive",
      safeFilename(meta.filename),
      meta.size,
      meta.mime_type ?? "application/octet-stream",
    );
    this.transfers.set(id, st);
    this.report(st, true);
  }

  private handleComplete(payload: TransferCompletePayload): void {
    const st = this.active(payload.transfer_id);
    if (!st) return;

    if (st.direction === "send") {
      // The receiver's confirmation: verified and saved
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

  /** Fail every active transfer without notifying the peer (channel gone). */
  close(reason = "connection closed"): void {
    for (const st of this.transfers.values()) {
      if (!st.finished) this.finish(st, "failed", reason, false);
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

  private fail(st: TransferState, reason: string): void {
    this.finish(st, "failed", reason, true);
  }

  private finish(st: TransferState, status: TransferStatus, reason: string, notify: boolean): void {
    if (st.finished) return;
    st.status = status;
    st.reason = reason;
    if (status !== "completed") {
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
    if (force || now - st.lastReport >= REPORT_INTERVAL_MS) {
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
