"""
Sessions over end-to-end encrypted channels (#29, #30, #32): two CLI
sessions joined by an in-memory link, inspecting what goes over the wire.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from apps.cli.identity import TrustStore
from apps.cli.session import Session, Ui
from packages.crypto.e2e import KIND_ENCRYPTED, Identity
from packages.transfer.manager import TransferStatus


class FakePeer:
    """One end of an in-memory connection, recording what it sends."""

    max_message_size = 65536

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.other: FakePeer | None = None
        self.closed = False
        self.wire: list[str | bytes] = []
        self.tamper: Callable[[str | bytes], str | bytes] | None = None

    def send(self, data: str | bytes) -> None:
        if self.closed:
            raise ConnectionError("closed")
        self.wire.append(data)
        assert self.other is not None
        self.other.inbox.put_nowait(self.tamper(data) if self.tamper else data)

    async def drain(self) -> None:
        if self.closed:
            raise ConnectionError("closed")
        await asyncio.sleep(0)

    async def recv(self) -> str | bytes | None:
        return await self.inbox.get()

    async def close(self) -> None:
        for end in (self, self.other):
            if end is not None and not end.closed:
                end.closed = True
                end.inbox.put_nowait(None)


def _pair() -> tuple[FakePeer, FakePeer]:
    a, b = FakePeer(), FakePeer()
    a.other, b.other = b, a
    return a, b


class Side:
    def __init__(self, peer: FakePeer, tmp: Path, name: str, identity: Identity) -> None:
        self.out = io.StringIO()
        self.trust = TrustStore(tmp / f"{name}-trusted.json")
        self.session = Session(
            peer,  # type: ignore[arg-type]
            tmp / name,
            Ui(self.out),
            name=name,
            identity=identity,
            trust=self.trust,
        )
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.session.start()
        self.task = asyncio.create_task(self.session.run_inbound())


@pytest.fixture
async def sides(tmp_path: Path) -> AsyncIterator[tuple[Side, Side, FakePeer, FakePeer]]:
    pa, pb = _pair()
    a = Side(pa, tmp_path, "alice", Identity.generate())
    b = Side(pb, tmp_path, "bob", Identity.generate())
    yield a, b, pa, pb
    for side in (a, b):
        if side.task:
            side.task.cancel()


async def _connect(a: Side, b: Side) -> None:
    a.start()
    b.start()
    await asyncio.wait_for(
        asyncio.gather(asyncio.shield(a.session.peer_hello), asyncio.shield(b.session.peer_hello)),
        5,
    )


def _plaintext_types(wire: list[str | bytes]) -> list[str]:
    return [json.loads(m)["type"] for m in wire if isinstance(m, str)]


async def test_everything_after_the_exchange_is_encrypted(sides):
    a, b, pa, pb = sides
    await _connect(a, b)

    a.session.send_chat("the secret plan")
    await asyncio.sleep(0.05)

    assert "alice: the secret plan" in b.out.getvalue()
    # Only hello and key_exchange ever went in the clear
    assert _plaintext_types(pa.wire) == ["hello", "key_exchange"]
    encrypted = [m for m in pa.wire if isinstance(m, bytes)]
    assert encrypted and all(m[0] == KIND_ENCRYPTED for m in encrypted)
    assert not any(b"secret plan" in m for m in encrypted)
    # Both see the same code to compare
    assert a.session.sas == b.session.sas and len(a.session.sas) == 6


async def test_file_transfer_through_encryption(sides, tmp_path: Path):
    a, b, pa, _ = sides
    await _connect(a, b)
    src = tmp_path / "doc.bin"
    src.write_bytes(bytes(range(256)) * 4000)

    sent = await a.session.transfers.send_file(src, **a.session.send_options())

    assert sent.status is TransferStatus.COMPLETED, sent.reason
    assert (tmp_path / "bob" / "doc.bin").read_bytes() == src.read_bytes()
    assert not any(bytes(range(256))[:64] in m for m in pa.wire if isinstance(m, bytes))


async def test_peer_without_encryption_is_refused(sides):
    a, b, pa, pb = sides
    a.start()
    # bob is an old client: hello without e2e
    pb.send(
        json.dumps(
            {"version": 1, "id": "x", "type": "hello", "payload": {"capabilities": ["chat"]}}
        )
    )
    await asyncio.wait_for(a.task, 5)  # type: ignore[arg-type]

    assert a.session.refused
    assert "doesn't support end-to-end encryption" in a.out.getvalue()


async def test_chat_before_the_exchange_is_refused(sides):
    a, b, pa, pb = sides
    a.start()
    pb.send(json.dumps({"version": 1, "id": "x", "type": "chat", "payload": {"text": "hi"}}))
    await asyncio.wait_for(a.task, 5)  # type: ignore[arg-type]
    assert a.session.refused
    assert "hi" not in a.out.getvalue().split("before encrypting")[0].split(":")[-1]


async def test_tampering_drops_the_connection(sides):
    a, b, pa, pb = sides
    await _connect(a, b)

    def flip(data: str | bytes) -> str | bytes:
        if isinstance(data, bytes):
            return data[:-1] + bytes([data[-1] ^ 1])
        return data

    pa.tamper = flip
    a.session.send_chat("hello")
    await asyncio.wait_for(b.task, 5)  # type: ignore[arg-type]
    assert "hello" not in b.out.getvalue().replace("alice", "")
    assert pb.closed


async def test_verification_is_remembered(tmp_path: Path):
    alice_id, bob_id = Identity.generate(), Identity.generate()
    for round_ in range(2):
        pa, pb = _pair()
        a = Side(pa, tmp_path, "alice", alice_id)
        b = Side(pb, tmp_path, "bob", bob_id)
        await _connect(a, b)
        if round_ == 0:
            assert not a.session.verified
            assert "Compare these with bob" in a.out.getvalue()
            a.session.mark_verified()
        else:
            assert a.session.verified  # same device, remembered
            assert "verified device" in a.out.getvalue()
        for side in (a, b):
            side.task.cancel()  # type: ignore[union-attr]


async def test_a_new_device_with_a_known_name_is_flagged(tmp_path: Path):
    alice_id = Identity.generate()
    pa, pb = _pair()
    a, b = Side(pa, tmp_path, "alice", alice_id), Side(pb, tmp_path, "bob", Identity.generate())
    await _connect(a, b)
    a.session.mark_verified()  # alice trusts "bob" on that device

    pa, pb = _pair()
    a = Side(pa, tmp_path, "alice", alice_id)
    impostor = Side(pb, tmp_path, "bob", Identity.generate())  # same name, new key
    await _connect(a, impostor)

    assert not a.session.verified
    assert "isn't the device you verified as bob" in a.out.getvalue()
