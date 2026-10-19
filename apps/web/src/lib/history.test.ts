import { describe, expect, it } from "vitest";

import { type HistoryStorage, MAX_ENTRIES, clearHistory, loadHistory, recordTransfer } from "./history";
import type { Transfer } from "./transfer";

function fakeStorage(): HistoryStorage {
  const data = new Map<string, string>();
  return {
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  };
}

const transfer = (overrides: Partial<Transfer> = {}): Transfer => ({
  id: "t",
  direction: "receive",
  filename: "a.txt",
  size: 10,
  bytesDone: 10,
  status: "completed",
  reason: "",
  startedAt: 0,
  resumedFrom: 0,
  bytesAcked: 10,
  acked: true,
  interrupted: false,
  ...overrides,
});

describe("history", () => {
  it("records finished transfers, newest first", () => {
    const s = fakeStorage();
    recordTransfer(transfer({ filename: "first" }), s);
    recordTransfer(transfer({ filename: "second", status: "failed", reason: "boom" }), s);

    const entries = loadHistory(s);
    expect(entries.map((e) => e.filename)).toEqual(["second", "first"]);
    expect(entries[0]).toMatchObject({ status: "failed", reason: "boom" });
  });

  it("ignores transfers still running", () => {
    const s = fakeStorage();
    recordTransfer(transfer({ status: "streaming" }), s);
    expect(loadHistory(s)).toEqual([]);
  });

  it("keeps at most MAX_ENTRIES", () => {
    const s = fakeStorage();
    for (let i = 0; i < MAX_ENTRIES + 5; i++) recordTransfer(transfer({ filename: `f${i}` }), s);
    const entries = loadHistory(s);
    expect(entries).toHaveLength(MAX_ENTRIES);
    expect(entries[0]!.filename).toBe(`f${MAX_ENTRIES + 4}`);
  });

  it("survives corrupt or missing storage", () => {
    const s = fakeStorage();
    s.setItem("whoosh.history", "{not json");
    expect(loadHistory(s)).toEqual([]);
    expect(loadHistory(undefined)).toEqual([]);
    expect(() => recordTransfer(transfer(), undefined)).not.toThrow();
  });

  it("clears", () => {
    const s = fakeStorage();
    recordTransfer(transfer(), s);
    clearHistory(s);
    expect(loadHistory(s)).toEqual([]);
  });
});
