import { describe, expect, it } from "vitest";

import { SendQueue } from "./queue";

const file = (name: string) => new File(["x"], name);

/** A send function whose calls finish only when the test says so. */
function controllableSend() {
  const started: string[] = [];
  const finishers: (() => void)[] = [];
  const send = (f: File) =>
    new Promise<void>((resolve) => {
      started.push(f.name);
      finishers.push(resolve);
    });
  const finishNext = async () => {
    finishers.shift()!();
    await new Promise((r) => setTimeout(r, 0));
  };
  return { send, started, finishNext };
}

describe("SendQueue", () => {
  it("sends one file at a time, in order", async () => {
    const { send, started, finishNext } = controllableSend();
    const q = new SendQueue(send);

    q.add([file("a"), file("b")]);
    q.add([file("c")]);
    await new Promise((r) => setTimeout(r, 0));
    expect(started).toEqual(["a"]);
    expect(q.waiting.map((i) => i.file.name)).toEqual(["b", "c"]);

    await finishNext();
    expect(started).toEqual(["a", "b"]);
    await finishNext();
    await finishNext();
    expect(started).toEqual(["a", "b", "c"]);
    expect(q.waiting).toEqual([]);
  });

  it("lets waiting files be removed", async () => {
    const { send, started, finishNext } = controllableSend();
    const q = new SendQueue(send);
    q.add([file("a"), file("b"), file("c")]);

    expect(q.remove(q.waiting[0]!.id)).toBe(true);
    await finishNext();
    await finishNext();

    expect(started).toEqual(["a", "c"]);
  });

  it("keeps going after a send throws", async () => {
    const sent: string[] = [];
    const q = new SendQueue(async (f) => {
      sent.push(f.name);
      if (f.name === "a") throw new Error("boom");
    });
    q.add([file("a"), file("b")]);
    await new Promise((r) => setTimeout(r, 10));
    expect(sent).toEqual(["a", "b"]);
  });

  it("reports the waiting list on every change", async () => {
    const { send, finishNext } = controllableSend();
    const seen: number[] = [];
    const q = new SendQueue(send, (waiting) => seen.push(waiting.length));
    q.add([file("a"), file("b")]);
    await finishNext();
    expect(seen).toEqual([2, 1, 0]);
    expect(q.clear()).toEqual([]);
  });
});
