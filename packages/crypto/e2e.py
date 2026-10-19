"""
End-to-end encryption between two peers (#29, #30, #32; docs/decisions.md
D60–D62). Spec "whoosh-e2e-v1" — apps/web/src/lib/e2e.ts must match it
byte for byte (shared test vectors in both suites):

Identity      Ed25519 key pair per device, kept across sessions.
              fingerprint = hex(SHA-256(identity_pub))[:32], shown as 8 groups of 4.
Exchange      Each side sends key_exchange {ephemeral, identity, signature}:
                ephemeral = fresh X25519 public key (32 bytes, base64)
                signature = Ed25519(identity, "whoosh-e2e-v1/kx" || ephemeral)
Keys          block(side)  = identity_pub || ephemeral_pub        (64 bytes)
              transcript   = SHA-256("whoosh-e2e-v1/transcript" || min(blocks) || max(blocks))
              shared       = X25519(my ephemeral, their ephemeral)
              okm          = HKDF-SHA256(shared, salt=transcript, info="whoosh-e2e-v1/keys", 64)
              The side whose block sorts first sends with okm[:32] and
              receives with okm[32:]; the other side the reverse.
SAS           6 emoji: the first 36 bits of SHA-256("whoosh-e2e-v1/sas" || transcript),
              6 bits each, indexing SAS_EMOJI.
Frames        After the exchange, every message travels as a binary frame:
                0x02 || counter (8 bytes, big-endian) || AES-256-GCM(key, nonce, plaintext, aad)
                nonce = 4 zero bytes || counter;  aad = "whoosh-e2e-v1"
                plaintext = 0x00 || utf-8 text   or   0x01 || binary payload
              Counters start at 0 per direction; the channel is ordered, so a
              receiver accepts only the exact next counter.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = b"whoosh-e2e-v1"
KIND_ENCRYPTED = 0x02
_HEADER = struct.Struct("!BQ")  # kind, counter

#: Matrix's SAS emoji table (64 entries, 6 bits each), widely used and
#: chosen to be distinguishable and nameable aloud.
SAS_EMOJI: tuple[tuple[str, str], ...] = (
    ("🐶", "dog"), ("🐱", "cat"), ("🦁", "lion"), ("🐎", "horse"), ("🦄", "unicorn"),
    ("🐷", "pig"), ("🐘", "elephant"), ("🐰", "rabbit"), ("🐼", "panda"), ("🐓", "rooster"),
    ("🐧", "penguin"), ("🐢", "turtle"), ("🐟", "fish"), ("🐙", "octopus"), ("🦋", "butterfly"),
    ("🌷", "flower"), ("🌳", "tree"), ("🌵", "cactus"), ("🍄", "mushroom"), ("🌏", "globe"),
    ("🌙", "moon"), ("☁️", "cloud"), ("🔥", "fire"), ("🍌", "banana"), ("🍎", "apple"),
    ("🍓", "strawberry"), ("🌽", "corn"), ("🍕", "pizza"), ("🎂", "cake"), ("❤️", "heart"),
    ("😀", "smiley"), ("🤖", "robot"), ("🎩", "hat"), ("👓", "glasses"), ("🔧", "spanner"),
    ("🎅", "santa"), ("👍", "thumbs up"), ("☂️", "umbrella"), ("⌛", "hourglass"), ("⏰", "clock"),
    ("🎁", "gift"), ("💡", "light bulb"), ("📕", "book"), ("✏️", "pencil"), ("📎", "paperclip"),
    ("✂️", "scissors"), ("🔒", "lock"), ("🔑", "key"), ("🔨", "hammer"), ("☎️", "telephone"),
    ("🏁", "flag"), ("🚂", "train"), ("🚲", "bicycle"), ("✈️", "aeroplane"), ("🚀", "rocket"),
    ("🏆", "trophy"), ("⚽", "ball"), ("🎸", "guitar"), ("🎺", "trumpet"), ("🔔", "bell"),
    ("⚓", "anchor"), ("🎧", "headphones"), ("📁", "folder"), ("📌", "pin"),
)  # fmt: skip


class E2EError(Exception):
    """The exchange or a frame failed verification. Never retry with the
    same keys: treat the connection as compromised."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(text: str, length: int) -> bytes:
    try:
        data = base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise E2EError("bad base64") from exc
    if len(data) != length:
        raise E2EError(f"expected {length} bytes, got {len(data)}")
    return data


def _raw(key: Ed25519PublicKey | X25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


# ── Identity ────────────────────────────────────────────────────────


class Identity:
    """A device's long-term signing key (#30)."""

    def __init__(self, private: Ed25519PrivateKey) -> None:
        self._private = private
        self.public = _raw(private.public_key())

    @classmethod
    def generate(cls) -> Identity:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_bytes(cls, raw: bytes) -> Identity:
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    def to_bytes(self) -> bytes:
        return self._private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public)

    def sign(self, data: bytes) -> bytes:
        return self._private.sign(data)


def fingerprint(identity_public: bytes) -> str:
    """32 hex digits of SHA-256(identity), in groups of 4."""
    digest = hashlib.sha256(identity_public).hexdigest()[:32]
    return " ".join(digest[i : i + 4] for i in range(0, 32, 4))


# ── Exchange ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KeyExchangeMessage:
    ephemeral: str
    identity: str
    signature: str


@dataclass(frozen=True)
class Established:
    """The outcome of a completed exchange."""

    sealer: Sealer
    #: The peer's long-term identity, verified by its signature.
    peer_identity: bytes
    sas_bytes: bytes

    @property
    def peer_fingerprint(self) -> str:
        return fingerprint(self.peer_identity)

    @property
    def sas(self) -> list[tuple[str, str]]:
        return sas_emoji(self.sas_bytes)


class KeyExchange:
    """One side of the exchange for one connection."""

    def __init__(self, identity: Identity, ephemeral: X25519PrivateKey | None = None) -> None:
        self._identity = identity
        self._ephemeral = ephemeral or X25519PrivateKey.generate()
        self.ephemeral_public = _raw(self._ephemeral.public_key())

    def message(self) -> KeyExchangeMessage:
        signature = self._identity.sign(VERSION + b"/kx" + self.ephemeral_public)
        return KeyExchangeMessage(
            ephemeral=_b64(self.ephemeral_public),
            identity=_b64(self._identity.public),
            signature=_b64(signature),
        )

    def complete(self, peer: KeyExchangeMessage) -> Established:
        """Verify the peer's message and derive the session. Raises E2EError."""
        their_eph = _unb64(peer.ephemeral, 32)
        their_id = _unb64(peer.identity, 32)
        signature = _unb64(peer.signature, 64)
        try:
            Ed25519PublicKey.from_public_bytes(their_id).verify(
                signature, VERSION + b"/kx" + their_eph
            )
        except InvalidSignature as exc:
            raise E2EError("the peer's key exchange signature is invalid") from exc

        mine = self._identity.public + self.ephemeral_public
        theirs = their_id + their_eph
        if mine == theirs:
            raise E2EError("the peer sent our own keys back")
        first, second = sorted((mine, theirs))
        transcript = hashlib.sha256(VERSION + b"/transcript" + first + second).digest()

        shared = self._ephemeral.exchange(X25519PublicKey.from_public_bytes(their_eph))
        if shared == bytes(32):
            raise E2EError("degenerate key exchange")
        okm = HKDF(SHA256(), 64, salt=transcript, info=VERSION + b"/keys").derive(shared)
        k1, k2 = okm[:32], okm[32:]
        send, receive = (k1, k2) if mine == first else (k2, k1)

        sas_bytes = hashlib.sha256(VERSION + b"/sas" + transcript).digest()[:5]
        return Established(Sealer(send, receive), their_id, sas_bytes)


def sas_emoji(sas_bytes: bytes) -> list[tuple[str, str]]:
    """6 emoji from the first 36 bits."""
    bits = int.from_bytes(sas_bytes[:5], "big") >> 4  # 40 bits -> top 36
    return [SAS_EMOJI[(bits >> (6 * (5 - i))) & 0x3F] for i in range(6)]


# ── Frames ──────────────────────────────────────────────────────────


class Sealer:
    """Encrypts outgoing and decrypts incoming messages for one connection."""

    def __init__(self, send_key: bytes, receive_key: bytes) -> None:
        self._send = AESGCM(send_key)
        self._receive = AESGCM(receive_key)
        self._send_counter = 0
        self._receive_counter = 0

    def seal(self, message: str | bytes) -> bytes:
        plaintext = b"\x00" + message.encode() if isinstance(message, str) else b"\x01" + message
        counter = self._send_counter
        self._send_counter += 1
        nonce = bytes(4) + counter.to_bytes(8, "big")
        return _HEADER.pack(KIND_ENCRYPTED, counter) + self._send.encrypt(nonce, plaintext, VERSION)

    def open(self, frame: bytes) -> str | bytes:
        """Decrypt one frame. Raises E2EError on anything unexpected."""
        if len(frame) < _HEADER.size + 16 + 1:
            raise E2EError("encrypted frame too short")
        kind, counter = _HEADER.unpack_from(frame)
        if kind != KIND_ENCRYPTED:
            raise E2EError("not an encrypted frame")
        if counter != self._receive_counter:
            raise E2EError(f"expected frame {self._receive_counter}, got {counter}")
        nonce = bytes(4) + counter.to_bytes(8, "big")
        try:
            plaintext = self._receive.decrypt(nonce, frame[_HEADER.size :], VERSION)
        except InvalidTag as exc:
            raise E2EError("an encrypted frame failed authentication") from exc
        self._receive_counter += 1
        if plaintext[:1] == b"\x00":
            return plaintext[1:].decode()
        if plaintext[:1] == b"\x01":
            return plaintext[1:]
        raise E2EError("unknown encrypted payload type")
