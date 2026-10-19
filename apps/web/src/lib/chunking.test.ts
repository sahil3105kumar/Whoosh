/** Mirrors tests/transfer/test_chunking.py. */
import { describe, expect, it } from "vitest";

import { ChunkSizer, MAX_CHUNK, MIN_CHUNK, WINDOW_MS } from "./chunking";
import { HEADER_SIZE } from "./frames";

function run(sizer: ChunkSizer, rateFor: (size: number) => number, windows = 40): number[] {
  let now = 0;
  const sizes: number[] = [];
  for (let i = 0; i < windows; i++) {
    const rate = rateFor(sizer.size); // bytes per ms
    const bytes = Math.floor(rate * WINDOW_MS * 1.01);
    now += bytes / rate;
    sizer.sent(bytes, now);
    sizes.push(sizer.size);
  }
  return sizes;
}

describe("ChunkSizer", () => {
  it("starts small", () => {
    expect(new ChunkSizer(65536).size).toBe(MIN_CHUNK);
  });

  it("grows while bigger chunks are faster", () => {
    const sizes = run(new ChunkSizer(1 << 20), (size) => (1000 * size) / (size + 20_000));
    expect(sizes.at(-1)).toBe(MAX_CHUNK);
  });

  it("holds on a plateau", () => {
    const sizes = run(new ChunkSizer(1 << 20), (size) => Math.min(size, 32 * 1024));
    expect(sizes.at(-1)).toBe(64 * 1024);
    expect(new Set(sizes.slice(-10)).size).toBe(1);
  });

  it("never exceeds the connection limit", () => {
    const sizes = run(new ChunkSizer(65536), (size) => size);
    expect(Math.max(...sizes)).toBe(65536 - HEADER_SIZE);
  });
});
