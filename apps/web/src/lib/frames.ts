/**
 * Binary frame format for file data — must match packages/transfer/frames.py.
 *
 *   offset  size  field
 *   0       1     kind         0x01 = file chunk
 *   1       16    transfer_id  UUID bytes
 *   17      8     offset       byte offset in the file, big-endian
 *   25      ...   data
 */

export const KIND_CHUNK = 0x01;
export const CHUNK_SIZE = 16 * 1024;
export const HEADER_SIZE = 25;

export interface ChunkFrame {
  transferId: string; // 32-char lowercase hex, no dashes
  offset: number;
  data: Uint8Array;
}

export class FrameDecodeError extends Error {}

/** Normalize a UUID (dashed or not) to 32-char lowercase hex. */
export function uuidHex(id: string): string {
  const hex = id.replaceAll("-", "").toLowerCase();
  if (!/^[0-9a-f]{32}$/.test(hex)) throw new Error(`not a UUID: ${id}`);
  return hex;
}

export function newTransferId(): string {
  return uuidHex(crypto.randomUUID());
}

export function encodeChunk(transferId: string, offset: number, data: Uint8Array): Uint8Array {
  const frame = new Uint8Array(HEADER_SIZE + data.length);
  const view = new DataView(frame.buffer);
  frame[0] = KIND_CHUNK;
  const hex = uuidHex(transferId);
  for (let i = 0; i < 16; i++) frame[1 + i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  view.setBigUint64(17, BigInt(offset));
  frame.set(data, HEADER_SIZE);
  return frame;
}

export function decodeFrame(buffer: ArrayBuffer): ChunkFrame {
  if (buffer.byteLength < HEADER_SIZE) {
    throw new FrameDecodeError(`frame too short (${buffer.byteLength} bytes)`);
  }
  const bytes = new Uint8Array(buffer);
  if (bytes[0] !== KIND_CHUNK) {
    throw new FrameDecodeError(`unknown frame kind 0x${bytes[0]!.toString(16).padStart(2, "0")}`);
  }
  let transferId = "";
  for (let i = 1; i < 17; i++) transferId += bytes[i]!.toString(16).padStart(2, "0");
  const offset = new DataView(buffer).getBigUint64(17);
  if (offset > BigInt(Number.MAX_SAFE_INTEGER)) throw new FrameDecodeError("offset too large");
  return { transferId, offset: Number(offset), data: bytes.subarray(HEADER_SIZE) };
}
