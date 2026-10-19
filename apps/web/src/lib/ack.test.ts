/**
 * Chunk acknowledgements in the web client — mirrors tests/transfer/test_ack.py.
 */
import { describe, expect, it } from "vitest";

import { CHUNK_SIZE } from "./frames";
import { decodeMessage } from "./protocol";
import { ACK_EVERY, ACK_WINDOW, type Channel, type Transfer, TransferManager } from "./transfer";

type Tamper = (data: string | Uint8Array) => string | Uint8Array | null;

class MemoryChannel implements Channel {
  peer?: TransferManager;
  tamper?: Tamper;
  sent: (string | Uint8Array)[] = [];

  send(data: string | Uint8Array): void {
    this.sent.push(data);
    const out = this.tamper ? this.tamper(data) : data;
    if (out === null) return;
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

function pair(stallTimeoutMs = 20_000) {
  const a = new MemoryChannel();
  const b = new MemoryChannel();
  const senderUpdates: Transfer[] = [];
  const sender = new TransferManager(a, (t) => senderUpdates.push(t), {
    partials: new Map(),
    stallTimeoutMs,
    reportIntervalMs: 0,
  });
  const receiver = new TransferManager(b, () => {}, { partials: new Map() });
  a.peer = receiver;
  b.peer = sender;
  return { a, b, sender, senderUpdates };
}

const file = (size: number) => new File([new Uint8Array(size)], "data.bin");

const acks = (sent: (string | Uint8Array)[]) =>
  sent
    .filter((m): m is string => typeof m === "string")
    .map((m) => JSON.parse(m) as { type: string; payload: { offset: number } })
    .filter((m) => m.type === "transfer_ack")
    .map((m) => m.payload.offset);

describe("acknowledgements", () => {
  it("are sent every MiB and tracked by the sender", async () => {
    const { b, sender } = pair();
    const size = 5 * ACK_EVERY + 123;

    const sent = await sender.sendFile(file(size), { ack: true });

    expect(sent.status).toBe("completed");
    expect(acks(b.sent)).toHaveLength(5);
    expect(sent.bytesAcked).toBe(size);
  });

  it("aren't sent unless the sender asks", async () => {
    const { b, sender } = pair();
    await sender.sendFile(file(3 * ACK_EVERY));
    expect(acks(b.sent)).toEqual([]);
  });

  it("stall the sender when they stop arriving", async () => {
    const { b, sender } = pair(200);
    b.tamper = (data) => (typeof data === "string" && data.includes('"transfer_ack"') ? null : data);

    const sent = await sender.sendFile(file(ACK_WINDOW + 4 * ACK_EVERY), { ack: true });

    expect(sent.status).toBe("failed");
    expect(sent.reason).toMatch(/^stalled/);
    expect(sent.bytesAcked).toBe(0);
    expect(sent.bytesDone).toBeGreaterThanOrEqual(ACK_WINDOW);
    expect(sent.bytesDone).toBeLessThan(ACK_WINDOW + CHUNK_SIZE);
  });

  it("outside the sent range are ignored", async () => {
    const { b, sender, senderUpdates } = pair();
    b.tamper = (data) => {
      if (typeof data !== "string" || !data.includes('"transfer_ack"')) return data;
      const msg = JSON.parse(data) as { payload: { offset: number } };
      msg.payload.offset = 10 * ACK_EVERY;
      return JSON.stringify(msg);
    };

    const sent = await sender.sendFile(file(3 * ACK_EVERY), { ack: true });

    expect(sent.status).toBe("completed");
    expect(senderUpdates.map((t) => t.bytesAcked)).not.toContain(10 * ACK_EVERY);
  });
});
