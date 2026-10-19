"""
Resume: interrupted transfers continue from the receiver's partial copy.

An interruption is simulated by closing both managers mid-stream, as a
dropped connection would; the retry uses fresh managers (a new session)
sharing the receiver's download directory.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from packages.transfer.frames import CHUNK_SIZE
from packages.transfer.manager import Transfer, TransferManager, TransferStatus
from packages.transfer.resume import PartialStore, resume_key
from tests.transfer.test_transfer import MemoryChannel, _pump

SIZE = 64 * CHUNK_SIZE  # 1 MiB


class Session:
    """Sender and receiver managers joined by in-memory channels."""

    def __init__(self, downloads: Path, on_receive: Callable[[Transfer], None] | None = None):
        self.a, self.b = MemoryChannel(), MemoryChannel()
        self.sender = TransferManager(self.a, downloads.parent / "unused", accept_timeout=2.0)
        self.receiver = TransferManager(self.b, downloads, on_update=on_receive)
        self.pumps = [
            asyncio.create_task(_pump(self.a, self.receiver)),
            asyncio.create_task(_pump(self.b, self.sender)),
        ]

    def drop_connection(self) -> None:
        """What a lost DataChannel looks like to both managers."""

        def gone(_: str | bytes) -> str | bytes | None:
            raise ConnectionError("channel closed")

        self.a.tamper = self.b.tamper = gone
        self.receiver.close()
        self.sender.close()

    async def stop(self) -> None:
        for task in self.pumps:
            task.cancel()
        await asyncio.gather(*self.pumps, return_exceptions=True)


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    return tmp_path / "downloads"


@pytest.fixture
def src(tmp_path: Path) -> Path:
    path = tmp_path / "data.bin"
    path.write_bytes(bytes(i % 251 for i in range(SIZE)))
    return path


async def _interrupt_after(downloads: Path, src: Path, nbytes: int) -> Transfer:
    """Send ``src`` and drop the connection once ``nbytes`` have arrived."""
    session: Session

    def on_receive(t: Transfer) -> None:
        if t.bytes_done >= nbytes and not t.status.finished:
            session.drop_connection()

    session = Session(downloads, on_receive)
    try:
        return await session.sender.send_file(src, resume=True)
    finally:
        await session.stop()


async def _send(downloads: Path, src: Path, *, resume: bool = True) -> tuple[Transfer, Transfer]:
    session = Session(downloads)
    try:
        sent = await session.sender.send_file(src, resume=resume)
        [received] = session.receiver.transfers
        return sent, received
    finally:
        await session.stop()


@pytest.fixture
def no_report_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    from packages.transfer import manager

    monkeypatch.setattr(manager, "_REPORT_INTERVAL", 0)


# ── Resume key ──────────────────────────────────────────────────────


def _with_mtime(path: Path, ms: int) -> Path:
    os.utime(path, ns=(ms * 1_000_000, ms * 1_000_000))
    return path


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        # Shared with apps/web/src/lib/resume.test.ts: both clients must agree
        (3 * 1024 * 1024 + 5, "7d14564f74c4097ecd0752f7e32b165268a4ae4f055b49c6acaed70b98723057"),
        (None, "43d48a58e74088dd29dad64c7f924306b2d00504cf014c88a0e68fcaad4693b6"),
    ],
)
def test_resume_key_reference_vectors(tmp_path: Path, size: int | None, expected: str):
    path = tmp_path / "f"
    path.write_bytes(b"tiny" if size is None else bytes(i % 251 for i in range(size)))
    assert resume_key(_with_mtime(path, 1_700_000_000_123)) == expected


def test_resume_key_changes_with_content_or_mtime(tmp_path: Path):
    path = tmp_path / "f"
    path.write_bytes(b"a" * 3_000_000)
    original = resume_key(_with_mtime(path, 1000))

    assert resume_key(_with_mtime(path, 2000)) != original
    path.write_bytes(b"a" * 2_999_999 + b"b")  # change in the sampled tail
    assert resume_key(_with_mtime(path, 1000)) != original


# ── Resume flow ─────────────────────────────────────────────────────


@pytest.mark.usefixtures("no_report_throttle")
async def test_interrupted_transfer_resumes(downloads: Path, src: Path):
    first = await _interrupt_after(downloads, src, SIZE // 3)
    assert first.status is TransferStatus.FAILED

    sent, received = await _send(downloads, src)

    assert sent.status is TransferStatus.COMPLETED, sent.reason
    assert received.status is TransferStatus.COMPLETED
    assert SIZE // 3 <= received.resumed_from < SIZE
    assert sent.resumed_from == received.resumed_from
    assert received.path.read_bytes() == src.read_bytes()
    assert sorted(p.name for p in downloads.iterdir()) == ["data.bin"]  # partial dir cleaned up


async def test_fresh_transfer_offers_resume_from_zero(downloads: Path, src: Path):
    sent, received = await _send(downloads, src)

    assert sent.status is TransferStatus.COMPLETED
    assert received.resumed_from == 0
    assert received.path.read_bytes() == src.read_bytes()


@pytest.mark.usefixtures("no_report_throttle")
async def test_without_resume_capability_nothing_is_kept(downloads: Path, src: Path):
    session: Session

    def on_receive(t: Transfer) -> None:
        if t.bytes_done >= SIZE // 3 and not t.status.finished:
            session.drop_connection()

    session = Session(downloads, on_receive)
    try:
        await session.sender.send_file(src, resume=False)
    finally:
        await session.stop()

    assert not (downloads / ".whoosh-partial").exists()
    _, received = await _send(downloads, src, resume=False)
    assert received.resumed_from == 0


@pytest.mark.usefixtures("no_report_throttle")
async def test_corrupted_partial_fails_then_restarts_clean(downloads: Path, src: Path):
    await _interrupt_after(downloads, src, SIZE // 2)
    [part] = (downloads / ".whoosh-partial").glob("*.part")
    data = bytearray(part.read_bytes())
    data[10] ^= 0xFF
    part.write_bytes(bytes(data))

    sent, received = await _send(downloads, src)
    assert received.resumed_from > 0
    assert received.status is TransferStatus.FAILED
    assert received.reason == "checksum mismatch"
    assert sent.status is TransferStatus.CANCELLED
    assert not (downloads / ".whoosh-partial").exists()  # bad partial discarded

    sent, received = await _send(downloads, src)
    assert received.resumed_from == 0
    assert received.path.read_bytes() == src.read_bytes()


@pytest.mark.usefixtures("no_report_throttle")
async def test_cancel_discards_partial(downloads: Path, src: Path):
    session: Session

    def on_receive(t: Transfer) -> None:
        if t.bytes_done >= SIZE // 3 and not t.status.finished:
            session.receiver.cancel(t.id)

    session = Session(downloads, on_receive)
    try:
        sent = await session.sender.send_file(src, resume=True)
    finally:
        await session.stop()

    assert sent.status is TransferStatus.CANCELLED
    assert not (downloads / ".whoosh-partial").exists()


@pytest.mark.usefixtures("no_report_throttle")
async def test_modified_source_starts_over(downloads: Path, src: Path):
    await _interrupt_after(downloads, src, SIZE // 2)
    _with_mtime(src, 1_234)  # same bytes, but no longer provably the same file

    sent, received = await _send(downloads, src)

    assert received.resumed_from == 0
    assert received.path.read_bytes() == src.read_bytes()


async def test_complete_partial_needs_no_chunks(downloads: Path, src: Path):
    """Connection lost after the last chunk but before transfer_complete."""
    store = PartialStore(downloads)
    with store.open(resume_key(src), SIZE, 0) as f:
        f.write(src.read_bytes())

    sent, received = await _send(downloads, src)

    assert received.resumed_from == SIZE
    assert sent.status is TransferStatus.COMPLETED
    assert received.path.read_bytes() == src.read_bytes()


async def test_oversized_partial_is_ignored(downloads: Path, src: Path):
    store = PartialStore(downloads)
    with store.open(resume_key(src), SIZE, 0) as f:
        f.write(b"x" * (SIZE + 1))

    _, received = await _send(downloads, src)

    assert received.resumed_from == 0
    assert received.path.read_bytes() == src.read_bytes()


async def test_sender_gives_up_without_accept(downloads: Path, src: Path):
    session = Session(downloads)
    session.b.tamper = lambda data: None  # receiver's replies are lost
    session.sender._accept_timeout = 0.1
    try:
        sent = await session.sender.send_file(src, resume=True)
    finally:
        await session.stop()

    assert sent.status is TransferStatus.FAILED
    assert "did not answer" in sent.reason


def test_cleanup_removes_old_partials(downloads: Path):
    store = PartialStore(downloads)
    store.open("a" * 64, 10, 0).close()
    old = store.data_path("a" * 64)
    os.utime(old, (0, 0))

    store.cleanup()

    assert not old.exists()
