/**
 * Local transfer history (#22, docs/decisions.md D54) — the browser side
 * of apps/cli/history.py. Kept in this browser's localStorage only; the
 * received files themselves aren't kept, just what happened.
 */
import type { Transfer } from "./transfer";

export interface HistoryEntry {
  time: string; // ISO 8601
  direction: "send" | "receive";
  filename: string;
  size: number;
  status: "completed" | "failed" | "cancelled";
  reason: string;
  resumedFrom: number;
}

const KEY = "whoosh.history";
export const MAX_ENTRIES = 200;

/** The bits of Storage this uses, so tests can pass a fake. */
export type HistoryStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function defaultStorage(): HistoryStorage | undefined {
  try {
    return localStorage;
  } catch {
    return undefined; // blocked (private mode, sandboxed frame)
  }
}

/** All entries, newest first. */
export function loadHistory(storage = defaultStorage()): HistoryEntry[] {
  try {
    const raw = storage?.getItem(KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as HistoryEntry[]) : [];
  } catch {
    return [];
  }
}

/** Record a finished transfer. Never throws: history is a convenience. */
export function recordTransfer(t: Transfer, storage = defaultStorage()): void {
  if (t.status === "streaming") return;
  const entry: HistoryEntry = {
    time: new Date().toISOString(),
    direction: t.direction,
    filename: t.filename,
    size: t.size,
    status: t.status,
    reason: t.reason,
    resumedFrom: t.resumedFrom,
  };
  try {
    storage?.setItem(KEY, JSON.stringify([entry, ...loadHistory(storage)].slice(0, MAX_ENTRIES)));
  } catch {
    // quota exceeded or storage blocked
  }
}

export function clearHistory(storage = defaultStorage()): void {
  try {
    storage?.removeItem(KEY);
  } catch {
    // storage blocked
  }
}
