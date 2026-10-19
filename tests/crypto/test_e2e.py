"""End-to-end encryption core (#29, #30, #32)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from packages.crypto.e2e import (
    SAS_EMOJI,
    E2EError,
    Identity,
    KeyExchange,
    KeyExchangeMessage,
)

VECTORS = json.loads((Path(__file__).parents[1] / "vectors" / "e2e.json").read_text())


def _pair():
    a, b = KeyExchange(Identity.generate()), KeyExchange(Identity.generate())
    return a.complete(b.message()), b.complete(a.message())


def test_both_sides_agree():
    a, b = _pair()
    assert a.sas == b.sas
    assert len(a.sas) == 6
    assert b.sealer.open(a.sealer.seal("hi")) == "hi"
    assert a.sealer.open(b.sealer.seal(b"\x00bin")) == b"\x00bin"


def test_reference_vectors():
    """Shared with apps/web/src/lib/e2e.test.ts: both clients must agree."""
    v = VECTORS
    a = KeyExchange(
        Identity.from_bytes(bytes.fromhex(v["identity_a_private"])),
        X25519PrivateKey.from_private_bytes(bytes.fromhex(v["ephemeral_a_private"])),
    )
    assert a.message().__dict__ == v["kx_a"]
    assert (
        Identity.from_bytes(bytes.fromhex(v["identity_a_private"])).fingerprint
        == v["fingerprint_a"]
    )
    established = a.complete(KeyExchangeMessage(**v["kx_b"]))
    assert established.sas_bytes.hex() == v["sas_bytes"]
    assert [name for _, name in established.sas] == v["sas"]
    assert established.sealer.seal("hello").hex() == v["a_to_b_frame0_text_hello"]
    assert established.sealer.seal(b"\x00\x01\x02").hex() == v["a_to_b_frame1_binary_000102"]


def test_forged_signature_is_rejected():
    a, b = KeyExchange(Identity.generate()), KeyExchange(Identity.generate())
    msg = b.message()
    forged = KeyExchangeMessage(
        ephemeral=KeyExchange(Identity.generate()).message().ephemeral,  # swapped key
        identity=msg.identity,
        signature=msg.signature,
    )
    with pytest.raises(E2EError, match="signature"):
        a.complete(forged)


def test_a_man_in_the_middle_shows_different_codes():
    """M relays between A and B with its own keys: each side completes a
    valid exchange, but with M — and the codes they compare differ."""
    a, b, m = (KeyExchange(Identity.generate()) for _ in range(3))
    a_view = a.complete(m.message())
    b_view = b.complete(m.message())
    assert a_view.sas != b_view.sas


def test_tampered_frame_is_rejected():
    a, b = _pair()
    frame = bytearray(a.sealer.seal("secret"))
    frame[-1] ^= 1
    with pytest.raises(E2EError, match="authentication"):
        b.sealer.open(bytes(frame))


def test_replayed_or_reordered_frames_are_rejected():
    a, b = _pair()
    first, second = a.sealer.seal("one"), a.sealer.seal("two")
    with pytest.raises(E2EError, match="expected frame 0"):
        b.sealer.open(second)
    assert b.sealer.open(first) == "one"
    with pytest.raises(E2EError, match="expected frame 1"):
        b.sealer.open(first)  # replay


def test_identity_round_trip_and_fingerprint():
    ident = Identity.generate()
    again = Identity.from_bytes(ident.to_bytes())
    assert again.public == ident.public
    assert len(ident.fingerprint.split()) == 8


def test_sas_table_is_64_distinct_entries():
    assert len(SAS_EMOJI) == 64
    assert len({e for e, _ in SAS_EMOJI}) == 64
    assert len({n for _, n in SAS_EMOJI}) == 64


def test_garbage_key_exchange_is_rejected():
    a = KeyExchange(Identity.generate())
    with pytest.raises(E2EError):
        a.complete(KeyExchangeMessage(ephemeral="!!", identity="x", signature="y"))
