/** Mirrors tests/crypto/test_e2e.py, including the shared vectors. */
import { describe, expect, it } from "vitest";

import vectors from "../../../../tests/vectors/e2e.json";
import {
  E2EError,
  KeyExchange,
  SAS_EMOJI,
  fingerprint,
  generateIdentity,
  identityFromPrivateBytes,
} from "./e2e";

const fromHex = (h: string) => Uint8Array.from(h.match(/../g)!, (b) => parseInt(b, 16));
const toHex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");

async function pair() {
  const a = await KeyExchange.create(await generateIdentity());
  const b = await KeyExchange.create(await generateIdentity());
  const [ma, mb] = [await a.message(), await b.message()];
  return [await a.complete(mb), await b.complete(ma)] as const;
}

describe("whoosh-e2e-v1", () => {
  it("matches the Python reference vectors byte for byte", async () => {
    const identity = await identityFromPrivateBytes(fromHex(vectors.identity_a_private));
    const a = await KeyExchange.create(identity, fromHex(vectors.ephemeral_a_private));

    expect(await a.message()).toEqual(vectors.kx_a);
    expect(await fingerprint(identity.publicKey)).toBe(vectors.fingerprint_a);
    const established = await a.complete(vectors.kx_b);
    expect(toHex(established.sasBytes)).toBe(vectors.sas_bytes);
    expect(established.sas.map(([, name]) => name)).toEqual(vectors.sas);
    expect(toHex(await established.sealer.seal("hello"))).toBe(vectors.a_to_b_frame0_text_hello);
    expect(toHex(await established.sealer.seal(new Uint8Array([0, 1, 2])))).toBe(
      vectors.a_to_b_frame1_binary_000102,
    );
  });

  it("decrypts what the Python side encrypted", async () => {
    const identity = await identityFromPrivateBytes(fromHex(vectors.identity_b_private));
    const b = await KeyExchange.create(identity, fromHex(vectors.ephemeral_b_private));
    const established = await b.complete(vectors.kx_a);
    expect(await established.sealer.open(fromHex(vectors.a_to_b_frame0_text_hello))).toBe("hello");
    expect([...((await established.sealer.open(fromHex(vectors.a_to_b_frame1_binary_000102))) as Uint8Array)]).toEqual([0, 1, 2]);
  });

  it("agrees on both sides and round-trips", async () => {
    const [a, b] = await pair();
    expect(a.sas).toEqual(b.sas);
    expect(await b.sealer.open(await a.sealer.seal("hi"))).toBe("hi");
  });

  it("gives a man in the middle away through different codes", async () => {
    const [a, b, m] = await Promise.all([0, 1, 2].map(async () => KeyExchange.create(await generateIdentity())));
    const aView = await a!.complete(await m!.message());
    const bView = await b!.complete(await m!.message());
    expect(aView.sas).not.toEqual(bView.sas);
  });

  it("rejects a forged signature", async () => {
    const a = await KeyExchange.create(await generateIdentity());
    const b = await KeyExchange.create(await generateIdentity());
    const other = await KeyExchange.create(await generateIdentity());
    const forged = { ...(await b.message()), ephemeral: (await other.message()).ephemeral };
    await expect(a.complete(forged)).rejects.toThrow(/signature/);
  });

  it("rejects tampered, replayed and reordered frames", async () => {
    const [a, b] = await pair();
    const one = await a.sealer.seal("one");
    const two = await a.sealer.seal("two");
    await expect(b.sealer.open(two)).rejects.toThrow(E2EError);

    const [c, d] = await pair();
    const frame = await c.sealer.seal("secret");
    frame[frame.length - 1]! ^= 1;
    await expect(d.sealer.open(frame)).rejects.toThrow(/authentication/);

    const [e, f] = await pair();
    const first = await e.sealer.seal("x");
    expect(await f.sealer.open(first)).toBe("x");
    await expect(f.sealer.open(first)).rejects.toThrow(/expected frame 1/);
    void one;
  });

  it("uses the same 64-emoji table as Python", () => {
    expect(SAS_EMOJI).toHaveLength(64);
    expect(new Set(SAS_EMOJI.map(([e]) => e)).size).toBe(64);
  });
});
