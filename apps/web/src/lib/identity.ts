/**
 * This device's identity and the devices it has verified (#30) — the
 * browser side of apps/cli/identity.py.
 *
 * The identity key pair lives in IndexedDB as a non-extractable CryptoKey:
 * this origin can sign with it, but nothing can read the private key out.
 * Verified devices are kept in localStorage.
 */
import { type Identity, generateIdentity } from "./e2e";

const DB_NAME = "whoosh";
const STORE = "keys";
const KEY = "identity";
const TRUSTED = "whoosh.trusted";

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function idb<T>(db: IDBDatabase, mode: IDBTransactionMode, op: (s: IDBObjectStore) => IDBRequest): Promise<T> {
  return new Promise((resolve, reject) => {
    const request = op(db.transaction(STORE, mode).objectStore(STORE));
    request.onsuccess = () => resolve(request.result as T);
    request.onerror = () => reject(request.error);
  });
}

/** This device's identity, created on first use. Falls back to one that
 * lasts only for this page if IndexedDB is unavailable (private mode). */
export async function loadIdentity(): Promise<Identity> {
  try {
    const db = await openDb();
    const stored = await idb<Identity | undefined>(db, "readonly", (s) => s.get(KEY));
    if (stored) return stored;
    const identity = await generateIdentity();
    await idb(db, "readwrite", (s) => s.put(identity, KEY));
    return identity;
  } catch {
    return generateIdentity();
  }
}

interface TrustEntry {
  name: string;
  verified: string;
}

/** Devices this user has verified by comparing codes (#32). */
export class TrustStore {
  private entries: Record<string, TrustEntry>;

  constructor(private readonly storage: Pick<Storage, "getItem" | "setItem"> | undefined = safeStorage()) {
    try {
      this.entries = JSON.parse(storage?.getItem(TRUSTED) ?? "{}") as Record<string, TrustEntry>;
    } catch {
      this.entries = {};
    }
  }

  isVerified(fingerprint: string): boolean {
    return fingerprint in this.entries;
  }

  verify(fingerprint: string, name: string): void {
    this.entries[fingerprint] = { name, verified: new Date().toISOString() };
    try {
      this.storage?.setItem(TRUSTED, JSON.stringify(this.entries));
    } catch {
      // storage blocked: verified for this page only
    }
  }

  /** Fingerprints verified under `name` (to spot a familiar name on a new device). */
  verifiedAs(name: string): string[] {
    return Object.entries(this.entries)
      .filter(([, e]) => e.name === name)
      .map(([fp]) => fp);
  }
}

function safeStorage(): Storage | undefined {
  try {
    return localStorage;
  } catch {
    return undefined;
  }
}
