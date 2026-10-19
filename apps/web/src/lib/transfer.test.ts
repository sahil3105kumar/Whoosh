/**
 * TransferManager end to end over an in-memory channel pair — mirrors
 * tests/transfer/test_transfer.py.
 */
import { sha256 } from "@noble/hashes/sha2.js";
import { describe, expect, it } from "vitest";

import { CHUNK_SIZE, encodeChunk } from "./frames";
import { decodeMessage, encodeMessage } from "./protocol";
import { type Channel, type Transfer, TransferManager } from "./transfer";

class MemoryChannel implements Channel {
  peer?: TransferManager;
  tamper?: (data: string | Uint8Array) => string | Uint8Array | null;

  send(data: string | Uint8Array): void {
    const out = this.tamper ? this.tamper(data) : data;
    if (out === null) return;
    // Deliver asynchronously and in order, like a real DataChannel
    const copy = typeof out === "string" ? out : out.slice().buffer;
    queueMicrotask(() => {
      if (typeof copy === "string") this.peer!.handleMessage(decodeMessage(copy));
      else this.peer!.handleFrame(copy);
    });
  }

  async drain(): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

function makePair(confirmTimeoutMs = 2000) {
  const a = new MemoryChannel();
  const b = new MemoryChannel();
  const updates: Transfer[] = [];
  const sender = new TransferManager(a, () => {}, confirmTimeoutMs);
  const receiver = new TransferManager(b, (t) => updates.push(t));
  a.peer = receiver;
  b.peer = sender;
  return { a, b, sender, receiver, updates, last: () => updates.at(-1)! };
}

function makeFile(size: number, name = "data.bin"): File {
  const bytes = new Uint8Array(size);
  for (let i = 0; i < size; i++) bytes[i] = i % 251;
  return new File([bytes], name, { type: "application/octet-stream" });
}

async function received(t: Transfer): Promise<Uint8Array> {
  return new Uint8Array(await (await fetch(t.url!)).arrayBuffer());
}

describe("TransferManager", () => {
  it.each([0, 1, CHUNK_SIZE, 3 * CHUNK_SIZE + 7, 9 * 1024 * 1024])(
    "sends and receives %i bytes intact",
    async (size) => {
      const { sender, last } = makePair();
      const file = makeFile(size);

      const sent = await sender.sendFile(file);

      expect(sent.status, sent.reason).toBe("completed");
      expect(last().status).toBe("completed");
      const data = await received(last());
      expect(sha256(data)).toEqual(sha256(new Uint8Array(await file.arrayBuffer())));
    },
  );

  it("fails both sides on a corrupted chunk", async () => {
    const { a, sender, last } = makePair();
    a.tamper = (data) => {
      if (typeof data === "string") return data;
      const copy = data.slice();
      copy[copy.length - 1]! ^= 0xff;
      return copy;
    };

    const sent = await sender.sendFile(makeFile(2 * CHUNK_SIZE));

    expect(sent.status).toBe("cancelled");
    expect(sent.reason).toBe("checksum mismatch");
    expect(last().status).toBe("failed");
    expect(last().url).toBeUndefined();
  });

  it("stops the sender when the receiver cancels", async () => {
    const { sender, receiver, updates } = makePair();
    const big = makeFile(200 * CHUNK_SIZE);
    const sending = sender.sendFile(big);

    // Cancel as soon as the receiver has some data
    while (!updates.some((t) => t.bytesDone > 0)) await new Promise((r) => setTimeout(r, 0));
    receiver.cancel(updates[0]!.id, "no thanks");
    const sent = await sending;

    expect(sent.status).toBe("cancelled");
    expect(sent.reason).toBe("no thanks");
    expect(sent.bytesDone).toBeLessThan(big.size);
  });

  it("times out without the receiver's confirmation", async () => {
    const { b, sender } = makePair(50);
    b.tamper = () => null; // receiver's replies are lost

    const sent = await sender.sendFile(makeFile(10));

    expect(sent.status).toBe("failed");
    expect(sent.reason).toContain("did not confirm");
  });

  it("sanitizes the peer's filename", () => {
    const { receiver, last } = makePair();
    const tid = "0123456789abcdef0123456789abcdef";
    receiver.handleMessage(
      decodeMessage(
        encodeMessage("file_metadata", { transfer_id: tid, filename: "../../escape.txt", size: 0 }),
      ),
    );
    expect(last().filename).toBe("escape.txt");
  });

  it("fails on an out-of-order chunk", () => {
    const { receiver, last } = makePair();
    const tid = "0123456789abcdef0123456789abcdef";
    receiver.handleMessage(
      decodeMessage(encodeMessage("file_metadata", { transfer_id: tid, filename: "x", size: 100 })),
    );
    receiver.handleFrame(encodeChunk(tid, 50, new Uint8Array(10)).buffer as ArrayBuffer);

    expect(last().status).toBe("failed");
    expect(last().reason).toContain("expected offset 0");
  });
});
