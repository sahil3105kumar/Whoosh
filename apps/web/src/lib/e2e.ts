/**
 * End-to-end encryption between two peers (#29, #30, #32) — the browser
 * side of packages/crypto/e2e.py, spec "whoosh-e2e-v1". Must match it byte
 * for byte; tests/vectors/e2e.json is checked by both test suites.
 *
 * Uses WebCrypto: X25519, Ed25519, HKDF-SHA256, AES-256-GCM.
 */

const VERSION = new TextEncoder().encode("whoosh-e2e-v1");
export const KIND_ENCRYPTED = 0x02;
const HEADER_SIZE = 9; // kind + counter
/** Bytes an encrypted frame adds: header + GCM tag + payload type. */
export const OVERHEAD = HEADER_SIZE + 16 + 1;

/** Matrix's SAS emoji table — identical to SAS_EMOJI in packages/crypto/e2e.py. */
export const SAS_EMOJI: readonly (readonly [string, string])[] = [
  ["🐶", "dog"], ["🐱", "cat"], ["🦁", "lion"], ["🐎", "horse"], ["🦄", "unicorn"],
  ["🐷", "pig"], ["🐘", "elephant"], ["🐰", "rabbit"], ["🐼", "panda"], ["🐓", "rooster"],
  ["🐧", "penguin"], ["🐢", "turtle"], ["🐟", "fish"], ["🐙", "octopus"], ["🦋", "butterfly"],
  ["🌷", "flower"], ["🌳", "tree"], ["🌵", "cactus"], ["🍄", "mushroom"], ["🌏", "globe"],
  ["🌙", "moon"], ["☁️", "cloud"], ["🔥", "fire"], ["🍌", "banana"], ["🍎", "apple"],
  ["🍓", "strawberry"], ["🌽", "corn"], ["🍕", "pizza"], ["🎂", "cake"], ["❤️", "heart"],
  ["😀", "smiley"], ["🤖", "robot"], ["🎩", "hat"], ["👓", "glasses"], ["🔧", "spanner"],
  ["🎅", "santa"], ["👍", "thumbs up"], ["☂️", "umbrella"], ["⌛", "hourglass"], ["⏰", "clock"],
  ["🎁", "gift"], ["💡", "light bulb"], ["📕", "book"], ["✏️", "pencil"], ["📎", "paperclip"],
  ["✂️", "scissors"], ["🔒", "lock"], ["🔑", "key"], ["🔨", "hammer"], ["☎️", "telephone"],
  ["🏁", "flag"], ["🚂", "train"], ["🚲", "bicycle"], ["✈️", "aeroplane"], ["🚀", "rocket"],
  ["🏆", "trophy"], ["⚽", "ball"], ["🎸", "guitar"], ["🎺", "trumpet"], ["🔔", "bell"],
  ["⚓", "anchor"], ["🎧", "headphones"], ["📁", "folder"], ["📌", "pin"],
];

export class E2EError extends Error {}

export interface KeyExchangeMessage {
  ephemeral: string;
  identity: string;
  signature: string;
}

// ── Bytes ─────────────────────────────────────────────────────────────

function concat(...parts: Uint8Array[]): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.length;
  }
  return out;
}

const encoder = new TextEncoder();
const label = (suffix: string) => concat(VERSION, encoder.encode(suffix));

export function toBase64(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes));
}

function fromBase64(text: string, length: number): Uint8Array<ArrayBuffer> {
  let bytes: Uint8Array<ArrayBuffer>;
  try {
    bytes = Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
  } catch {
    throw new E2EError("bad base64");
  }
  if (bytes.length !== length) throw new E2EError(`expected ${length} bytes, got ${bytes.length}`);
  return bytes;
}

function compare(a: Uint8Array, b: Uint8Array): number {
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    if (a[i] !== b[i]) return a[i]! - b[i]!;
  }
  return a.length - b.length;
}

async function sha256(data: Uint8Array<ArrayBuffer>): Promise<Uint8Array<ArrayBuffer>> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", data));
}

const hex = (bytes: Uint8Array) =>
  Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");

// ── Identity ──────────────────────────────────────────────────────────

/** A device's long-term signing key (#30). */
export interface Identity {
  privateKey: CryptoKey;
  publicKey: Uint8Array<ArrayBuffer>;
}

export async function generateIdentity(): Promise<Identity> {
  // Non-extractable: the private key can sign but can never be read out
  const pair = (await crypto.subtle.generateKey({ name: "Ed25519" }, false, [
    "sign",
    "verify",
  ])) as CryptoKeyPair;
  return {
    privateKey: pair.privateKey,
    publicKey: new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey)),
  };
}

/** PKCS#8 prefixes wrapping a raw 32-byte private key (RFC 8410). */
const PKCS8_PREFIX = {
  Ed25519: "302e020100300506032b657004220420",
  X25519: "302e020100300506032b656e04220420",
} as const;

/** Import a raw private key (test vectors only; real keys are generated). */
async function importRawPrivate(
  algorithm: "Ed25519" | "X25519",
  raw: Uint8Array<ArrayBuffer>,
): Promise<{ privateKey: CryptoKey; publicKey: Uint8Array<ArrayBuffer> }> {
  const prefix = Uint8Array.from(PKCS8_PREFIX[algorithm].match(/../g)!, (h) => parseInt(h, 16));
  const usages: KeyUsage[] = algorithm === "Ed25519" ? ["sign"] : ["deriveBits"];
  const privateKey = await crypto.subtle.importKey(
    "pkcs8",
    concat(prefix, raw),
    { name: algorithm },
    true,
    usages,
  );
  const jwk = await crypto.subtle.exportKey("jwk", privateKey);
  const x = atob(jwk.x!.replace(/-/g, "+").replace(/_/g, "/"));
  return { privateKey, publicKey: Uint8Array.from(x, (c) => c.charCodeAt(0)) };
}

export function identityFromPrivateBytes(raw: Uint8Array<ArrayBuffer>): Promise<Identity> {
  return importRawPrivate("Ed25519", raw);
}

/** 32 hex digits of SHA-256(identity), in groups of 4. */
export async function fingerprint(identityPublic: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = hex(await sha256(identityPublic)).slice(0, 32);
  return digest.match(/.{4}/g)!.join(" ");
}

// ── Exchange ──────────────────────────────────────────────────────────

export interface Established {
  sealer: Sealer;
  /** The peer's long-term identity, proven by its signature. */
  peerIdentity: Uint8Array<ArrayBuffer>;
  peerFingerprint: string;
  sasBytes: Uint8Array;
  sas: (readonly [string, string])[];
}

export class KeyExchange {
  private constructor(
    private readonly identity: Identity,
    private readonly ephemeralPrivate: CryptoKey,
    readonly ephemeralPublic: Uint8Array<ArrayBuffer>,
  ) {}

  static async create(identity: Identity, ephemeralRaw?: Uint8Array<ArrayBuffer>): Promise<KeyExchange> {
    if (ephemeralRaw) {
      const { privateKey, publicKey } = await importRawPrivate("X25519", ephemeralRaw);
      return new KeyExchange(identity, privateKey, publicKey);
    }
    const pair = (await crypto.subtle.generateKey({ name: "X25519" }, false, [
      "deriveBits",
    ])) as CryptoKeyPair;
    const publicKey = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
    return new KeyExchange(identity, pair.privateKey, publicKey);
  }

  async message(): Promise<KeyExchangeMessage> {
    const signature = new Uint8Array(
      await crypto.subtle.sign(
        { name: "Ed25519" },
        this.identity.privateKey,
        concat(label("/kx"), this.ephemeralPublic),
      ),
    );
    return {
      ephemeral: toBase64(this.ephemeralPublic),
      identity: toBase64(this.identity.publicKey),
      signature: toBase64(signature),
    };
  }

  /** Verify the peer's message and derive the session. Throws E2EError. */
  async complete(peer: KeyExchangeMessage): Promise<Established> {
    const theirEph = fromBase64(peer.ephemeral, 32);
    const theirId = fromBase64(peer.identity, 32);
    const signature = fromBase64(peer.signature, 64);
    const idKey = await crypto.subtle.importKey("raw", theirId, { name: "Ed25519" }, false, [
      "verify",
    ]);
    const valid = await crypto.subtle.verify(
      { name: "Ed25519" },
      idKey,
      signature,
      concat(label("/kx"), theirEph),
    );
    if (!valid) throw new E2EError("the peer's key exchange signature is invalid");

    const mine = concat(this.identity.publicKey, this.ephemeralPublic);
    const theirs = concat(theirId, theirEph);
    if (compare(mine, theirs) === 0) throw new E2EError("the peer sent our own keys back");
    const [first, second] = compare(mine, theirs) < 0 ? [mine, theirs] : [theirs, mine];
    const transcript = await sha256(concat(label("/transcript"), first, second));

    const ephKey = await crypto.subtle.importKey("raw", theirEph, { name: "X25519" }, false, []);
    const shared = new Uint8Array(
      await crypto.subtle.deriveBits({ name: "X25519", public: ephKey }, this.ephemeralPrivate, 256),
    );
    if (shared.every((b) => b === 0)) throw new E2EError("degenerate key exchange");
    const hkdfKey = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveBits"]);
    const okm = new Uint8Array(
      await crypto.subtle.deriveBits(
        { name: "HKDF", hash: "SHA-256", salt: transcript, info: label("/keys") },
        hkdfKey,
        512,
      ),
    );
    const [k1, k2] = [okm.slice(0, 32), okm.slice(32)];
    const [send, receive] = compare(mine, first) === 0 ? [k1, k2] : [k2, k1];

    const sasBytes = (await sha256(concat(label("/sas"), transcript))).slice(0, 5);
    return {
      sealer: await Sealer.create(send, receive),
      peerIdentity: theirId,
      peerFingerprint: await fingerprint(theirId),
      sasBytes,
      sas: sasEmoji(sasBytes),
    };
  }
}

/** 6 emoji from the first 36 bits. */
export function sasEmoji(sasBytes: Uint8Array): (readonly [string, string])[] {
  let bits = 0n;
  for (const b of sasBytes.slice(0, 5)) bits = (bits << 8n) | BigInt(b);
  bits >>= 4n; // 40 bits -> top 36
  return Array.from({ length: 6 }, (_, i) => SAS_EMOJI[Number((bits >> BigInt(6 * (5 - i))) & 0x3fn)]!);
}

// ── Frames ────────────────────────────────────────────────────────────

/** Encrypts outgoing and decrypts incoming messages for one connection. */
export class Sealer {
  private sendCounter = 0n;
  private receiveCounter = 0n;

  private constructor(
    private readonly sendKey: CryptoKey,
    private readonly receiveKey: CryptoKey,
  ) {}

  static async create(send: Uint8Array<ArrayBuffer>, receive: Uint8Array<ArrayBuffer>): Promise<Sealer> {
    const importKey = (raw: Uint8Array<ArrayBuffer>, usage: KeyUsage) =>
      crypto.subtle.importKey("raw", raw, "AES-GCM", false, [usage]);
    return new Sealer(await importKey(send, "encrypt"), await importKey(receive, "decrypt"));
  }

  /** Must be awaited in order: counters are assigned at call time. */
  async seal(message: string | Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
    const plaintext =
      typeof message === "string"
        ? concat(new Uint8Array([0]), encoder.encode(message))
        : concat(new Uint8Array([1]), message);
    const counter = this.sendCounter++;
    const header = new Uint8Array(HEADER_SIZE);
    header[0] = KIND_ENCRYPTED;
    new DataView(header.buffer).setBigUint64(1, counter);
    const ciphertext = new Uint8Array(
      await crypto.subtle.encrypt(
        { name: "AES-GCM", iv: nonce(counter), additionalData: VERSION },
        this.sendKey,
        plaintext,
      ),
    );
    return concat(header, ciphertext);
  }

  /** Decrypt one frame. Must be called in arrival order. Throws E2EError. */
  async open(frame: Uint8Array): Promise<string | Uint8Array<ArrayBuffer>> {
    if (frame.length < HEADER_SIZE + 17) throw new E2EError("encrypted frame too short");
    if (frame[0] !== KIND_ENCRYPTED) throw new E2EError("not an encrypted frame");
    const counter = new DataView(frame.buffer, frame.byteOffset).getBigUint64(1);
    if (counter !== this.receiveCounter) {
      throw new E2EError(`expected frame ${this.receiveCounter}, got ${counter}`);
    }
    this.receiveCounter++;
    let plaintext: Uint8Array<ArrayBuffer>;
    try {
      plaintext = new Uint8Array(
        await crypto.subtle.decrypt(
          { name: "AES-GCM", iv: nonce(counter), additionalData: VERSION },
          this.receiveKey,
          frame.slice(HEADER_SIZE),
        ),
      );
    } catch {
      throw new E2EError("an encrypted frame failed authentication");
    }
    if (plaintext[0] === 0) return new TextDecoder().decode(plaintext.subarray(1));
    if (plaintext[0] === 1) return plaintext.slice(1);
    throw new E2EError("unknown encrypted payload type");
  }
}

function nonce(counter: bigint): Uint8Array<ArrayBuffer> {
  const iv = new Uint8Array(12);
  new DataView(iv.buffer).setBigUint64(4, counter);
  return iv;
}
