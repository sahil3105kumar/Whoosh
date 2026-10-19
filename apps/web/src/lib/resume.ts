/**
 * Resume support — the browser side of packages/transfer/resume.py.
 *
 * The resume key must match the Python implementation byte for byte:
 *
 *   sha256(
 *     utf8("whoosh-resume-v1\n{size}\n{mtime_ms}\n")
 *     || file[0 : min(SAMPLE, size)]
 *     || file[max(0, size - SAMPLE) : size]
 *   ) as lowercase hex
 */
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

export const SAMPLE = 1024 * 1024;

export type Hasher = ReturnType<typeof sha256.create>;

export async function resumeKey(file: File): Promise<string> {
  const h = sha256.create();
  h.update(new TextEncoder().encode(`whoosh-resume-v1\n${file.size}\n${Math.floor(file.lastModified)}\n`));
  h.update(new Uint8Array(await file.slice(0, SAMPLE).arrayBuffer()));
  h.update(new Uint8Array(await file.slice(Math.max(0, file.size - SAMPLE)).arrayBuffer()));
  return bytesToHex(h.digest());
}

export function isResumeKey(value: string): boolean {
  return /^[0-9a-f]{64}$/.test(value);
}

/**
 * A partially received file, kept in memory so a later transfer of the
 * same file (in any room, while this tab is open) can continue it. The
 * hash state is kept too, so resuming doesn't re-read the data.
 */
export interface Partial {
  size: number;
  bytes: number;
  blobs: Blob[];
  hash: Hasher;
}

/** Partials for this tab, by resume key (docs/decisions.md D47). */
export const tabPartials = new Map<string, Partial>();
