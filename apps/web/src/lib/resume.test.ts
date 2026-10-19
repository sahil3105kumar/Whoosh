/**
 * Resume in the web client — mirrors tests/transfer/test_resume.py.
 */
import { describe, expect, it } from "vitest";

import { CHUNK_SIZE } from "./frames";
import { decodeMessage } from "./protocol";
import { type Partial, resumeKey } from "./resume";
import { type Channel, ConnectionClosedError, type Transfer, TransferManager } from "./transfer";

const SIZE = 64 * CHUNK_SIZE;

function pattern(size: number): Uint8Array<ArrayBuffer> {
  const bytes = new Uint8Array(size);
  for (let i = 0; i < size; i++) bytes[i] = i % 251;
  return bytes;
}

class MemoryChannel implements Channel {
  peer?: TransferManager;
  closed = false;

  send(data: string | Uint8Array): void {
    if (this.closed) throw new ConnectionClosedError();
    const copy = typeof data === "string" ? data : data.slice().buffer;
    queueMicrotask(() => {
      if (this.closed) return;
      if (typeof copy === "string") this.peer!.handleMessage(decodeMessage(copy));
      else this.peer!.handleFrame(copy);
    });
  }

  async drain(): Promise<void> {
    if (this.closed) throw new ConnectionClosedError();
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

/** Sender and receiver sharing one partial store, like two rooms in one tab. */
function session(partials: Map<string, Partial>, onReceive: (t: Transfer, s: Session) => void = () => {}) {
  const a = new MemoryChannel();
  const b = new MemoryChannel();
  const received: Transfer[] = [];
  const s: Session = {
    sender: new TransferManager(a, () => {}, { partials: new Map() }),
    receiver: undefined as unknown as TransferManager,
    received,
    drop() {
      a.closed = b.closed = true;
      s.receiver.close();
      s.sender.close();
    },
  };
  s.receiver = new TransferManager(
    b,
    (t) => {
      received.push(t);
      onReceive(t, s);
    },
    { partials, reportIntervalMs: 0 },
  );
  a.peer = s.receiver;
  b.peer = s.sender;
  return s;
}

interface Session {
  sender: TransferManager;
  receiver: TransferManager;
  received: Transfer[];
  drop(): void;
}

const file = () => new File([pattern(SIZE)], "data.bin", { lastModified: 1_000 });

async function bytesOf(t: Transfer): Promise<Uint8Array> {
  return new Uint8Array(await (await fetch(t.url!)).arrayBuffer());
}

describe("resume key", () => {
  // Same vectors as tests/transfer/test_resume.py: both clients must agree
  it.each([
    [pattern(3 * 1024 * 1024 + 5), "7d14564f74c4097ecd0752f7e32b165268a4ae4f055b49c6acaed70b98723057"],
    [new TextEncoder().encode("tiny") as Uint8Array<ArrayBuffer>, "43d48a58e74088dd29dad64c7f924306b2d00504cf014c88a0e68fcaad4693b6"],
  ])("matches the Python reference vector %#", async (bytes, expected) => {
    const f = new File([bytes as BlobPart], "f", { lastModified: 1_700_000_000_123 });
    expect(await resumeKey(f)).toBe(expected);
  });
});

describe("resume", () => {
  it("continues an interrupted transfer", async () => {
    const partials = new Map<string, Partial>();
    const first = session(partials, (t, s) => {
      if (t.bytesDone >= SIZE / 3 && t.status === "streaming") s.drop();
    });
    const interrupted = await first.sender.sendFile(file(), { resume: true });
    expect(interrupted.status).toBe("failed");
    expect(partials.size).toBe(1);

    const second = session(partials);
    const sent = await second.sender.sendFile(file(), { resume: true });
    const got = second.received.at(-1)!;

    expect(sent.status, sent.reason).toBe("completed");
    expect(got.resumedFrom).toBeGreaterThanOrEqual(SIZE / 3);
    expect(sent.resumedFrom).toBe(got.resumedFrom);
    expect(await bytesOf(got)).toEqual(pattern(SIZE));
    expect(partials.size).toBe(0);
  });

  it("keeps nothing when the peer doesn't support resume", async () => {
    const partials = new Map<string, Partial>();
    const s = session(partials, (t, s) => {
      if (t.bytesDone >= SIZE / 3 && t.status === "streaming") s.drop();
    });
    await s.sender.sendFile(file(), { resume: false });
    expect(partials.size).toBe(0);
  });

  it("discards the partial on cancel", async () => {
    const partials = new Map<string, Partial>();
    const s = session(partials, (t, s) => {
      if (t.bytesDone >= SIZE / 3 && t.status === "streaming") s.receiver.cancel(t.id);
    });
    const sent = await s.sender.sendFile(file(), { resume: true });
    expect(sent.status).toBe("cancelled");
    expect(partials.size).toBe(0);
  });

  it("starts over when the file was modified", async () => {
    const partials = new Map<string, Partial>();
    const first = session(partials, (t, s) => {
      if (t.bytesDone >= SIZE / 3 && t.status === "streaming") s.drop();
    });
    await first.sender.sendFile(file(), { resume: true });

    const second = session(partials);
    const modified = new File([pattern(SIZE)], "data.bin", { lastModified: 2_000 });
    await second.sender.sendFile(modified, { resume: true });

    expect(second.received.at(-1)!.resumedFrom).toBe(0);
    expect(await bytesOf(second.received.at(-1)!)).toEqual(pattern(SIZE));
  });

  it("gives up when the receiver never answers", async () => {
    const a = new MemoryChannel();
    a.peer = new TransferManager({ send() {}, drain: async () => {} });
    const sender = new TransferManager(a, () => {}, { acceptTimeoutMs: 50, partials: new Map() });
    const sent = await sender.sendFile(file(), { resume: true });
    expect(sent.status).toBe("failed");
    expect(sent.reason).toContain("did not answer");
  });
});
