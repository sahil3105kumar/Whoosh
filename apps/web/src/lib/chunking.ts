/**
 * Adaptive chunk sizing (#21) — the browser side of
 * packages/transfer/chunking.py, same constants and rules.
 *
 * Senders start small and grow chunks while throughput keeps improving.
 * Frames carry byte offsets, so receivers accept any size; the only limit
 * is the connection's maximum message size.
 */
import { CHUNK_SIZE, HEADER_SIZE } from "./frames";

export const MIN_CHUNK = CHUNK_SIZE;
export const MAX_CHUNK = 256 * 1024;
export const WINDOW_MS = 250;
const WINDOW_BYTES = 512 * 1024;
const GROW_IF_FASTER = 1.05;
const SHRINK_IF_SLOWER = 0.8;

export class ChunkSizer {
  readonly maxChunk: number;
  size = MIN_CHUNK;
  private windowStart?: number;
  private windowBytes = 0;
  private lastRate?: number;

  constructor(maxMessageSize: number) {
    this.maxChunk = Math.max(MIN_CHUNK, Math.min(MAX_CHUNK, maxMessageSize - HEADER_SIZE));
  }

  /** Record `bytes` sent at time `now` (ms); may change `size`. */
  sent(bytes: number, now: number): void {
    if (this.windowStart === undefined) {
      // Open the first window; these bytes left before it, don't count them
      this.windowStart = now;
      return;
    }
    this.windowBytes += bytes;
    const elapsed = now - this.windowStart;
    if (elapsed < WINDOW_MS || this.windowBytes < WINDOW_BYTES) return;
    this.adjust(this.windowBytes / elapsed);
    this.windowStart = now;
    this.windowBytes = 0;
  }

  private adjust(rate: number): void {
    const last = this.lastRate;
    this.lastRate = rate;
    if (last === undefined || rate >= last * GROW_IF_FASTER) {
      this.size = Math.min(this.size * 2, this.maxChunk);
    } else if (rate < last * SHRINK_IF_SLOWER) {
      this.size = Math.max(Math.floor(this.size / 2), MIN_CHUNK);
    }
  }
}
