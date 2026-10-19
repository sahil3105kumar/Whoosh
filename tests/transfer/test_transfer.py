"""
Tests for packages/transfer — frames, filenames, and full send/receive
between two TransferManagers over an in-memory channel (no WebRTC).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from packages.protocol.codec import decode
from packages.protocol.enums import MessageType
from packages.protocol.models import FileMetadataPayload, make_message
from packages.transfer import manager as manager_module
from packages.transfer.filenames import safe_filename, unique_path
from packages.transfer.frames import (
    CHUNK_SIZE,
    FrameDecodeError,
    decode_frame,
    encode_chunk,
)
from packages.transfer.manager import Direction, Transfer, TransferManager, TransferStatus

TID = "0123456789abcdef0123456789abcdef"

# ── Frames ──────────────────────────────────────────────────────────


def test_chunk_frame_round_trip():
    frame = decode_frame(encode_chunk(TID, 2**40, b"payload"))
    assert (frame.transfer_id, frame.offset, frame.data) == (TID, 2**40, b"payload")


def test_frame_accepts_dashed_uuid():
    dashed = "01234567-89ab-cdef-0123-456789abcdef"
    assert decode_frame(encode_chunk(dashed, 0, b"")).transfer_id == TID


@pytest.mark.parametrize("frame", [b"", b"\x01" * 10, b"\x02" + b"\x00" * 24])
def test_decode_rejects_bad_frames(frame: bytes):
    with pytest.raises(FrameDecodeError):
        decode_frame(frame)


# ── Filenames ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\evil.exe", "evil.exe"),
        ("..", "download"),
        ("", "download"),
        ("a\x00b\nc.txt", "a_b_c.txt"),
        ('what?:*"<>|.txt', "what_______.txt"),
    ],
)
def test_safe_filename(given: str, expected: str):
    assert safe_filename(given) == expected


def test_safe_filename_truncates_long_names_keeping_extension():
    name = safe_filename("x" * 400 + ".tar.gz")
    assert len(name.encode()) <= 255
    assert name.endswith(".gz")


def test_unique_path_skips_existing_files(tmp_path: Path):
    (tmp_path / "a.txt").touch()
    (tmp_path / "a (1).txt").touch()
    assert unique_path(tmp_path, "a.txt") == tmp_path / "a (2).txt"


# ── End-to-end over an in-memory channel ────────────────────────────


class MemoryChannel:
    """One end of an in-memory DataChannel: send() queues for the other side."""

    def __init__(self) -> None:
        self.outbox: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.tamper: Callable[[str | bytes], str | bytes | None] | None = None

    def send(self, data: str | bytes) -> None:
        if self.tamper is not None:
            tampered = self.tamper(data)
            if tampered is None:
                return  # dropped
            data = tampered
        self.outbox.put_nowait(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)  # yield like a real channel would


async def _pump(src: MemoryChannel, dest: TransferManager) -> None:
    while True:
        data = await src.outbox.get()
        if isinstance(data, bytes):
            dest.handle_frame(data)
        else:
            assert await dest.handle_message(decode(data.encode()))


@dataclass
class Pair:
    sender: TransferManager
    receiver: TransferManager
    sender_channel: MemoryChannel
    receiver_channel: MemoryChannel
    downloads: Path
    updates: list[tuple[TransferStatus, int]]  # receiver's (status, bytes_done) snapshots


@pytest.fixture
async def pair(tmp_path: Path) -> AsyncIterator[Pair]:
    a, b = MemoryChannel(), MemoryChannel()
    downloads = tmp_path / "downloads"
    updates: list[tuple[TransferStatus, int]] = []
    sender = TransferManager(a, tmp_path / "unused", confirm_timeout=2.0)
    receiver = TransferManager(
        b, downloads, on_update=lambda t: updates.append((t.status, t.bytes_done))
    )
    pumps = [asyncio.create_task(_pump(a, receiver)), asyncio.create_task(_pump(b, sender))]
    yield Pair(sender, receiver, a, b, downloads, updates)
    for task in pumps:
        task.cancel()


def _make_file(directory: Path, name: str, size: int) -> Path:
    path = directory / name
    path.write_bytes(bytes(i % 251 for i in range(size)))
    return path


def _received(pair: Pair) -> Transfer:
    [transfer] = pair.receiver.transfers
    return transfer


@pytest.mark.parametrize("size", [0, 1, CHUNK_SIZE, 3 * CHUNK_SIZE + 7])
async def test_send_and_receive(pair: Pair, tmp_path: Path, size: int):
    src = _make_file(tmp_path, "data.bin", size)

    sent = await pair.sender.send_file(src)

    assert sent.status is TransferStatus.COMPLETED, sent.reason
    received = _received(pair)
    assert received.status is TransferStatus.COMPLETED
    assert received.direction is Direction.RECEIVE
    assert received.path == pair.downloads / "data.bin"
    assert received.path.read_bytes() == src.read_bytes()
    assert list(pair.downloads.iterdir()) == [received.path]  # no .part left behind


async def test_existing_file_is_not_overwritten(pair: Pair, tmp_path: Path):
    pair.downloads.mkdir()
    (pair.downloads / "data.bin").write_bytes(b"keep me")
    src = _make_file(tmp_path, "data.bin", 100)

    await pair.sender.send_file(src)

    assert (pair.downloads / "data.bin").read_bytes() == b"keep me"
    assert _received(pair).path == pair.downloads / "data (1).bin"


async def test_corrupted_chunk_fails_checksum_on_both_sides(pair: Pair, tmp_path: Path):
    src = _make_file(tmp_path, "data.bin", 2 * CHUNK_SIZE)

    def flip_last_byte(data: str | bytes) -> str | bytes:
        if isinstance(data, bytes):
            return data[:-1] + bytes([data[-1] ^ 0xFF])
        return data

    pair.sender_channel.tamper = flip_last_byte
    sent = await pair.sender.send_file(src)

    assert sent.status is TransferStatus.CANCELLED
    assert sent.reason == "checksum mismatch"
    assert _received(pair).status is TransferStatus.FAILED
    assert list(pair.downloads.iterdir()) == []


async def test_receiver_cancel_stops_sender(
    pair: Pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(manager_module, "_REPORT_INTERVAL", 0)  # report every chunk
    src = _make_file(tmp_path, "big.bin", 50 * CHUNK_SIZE)

    def cancel_after_first_chunk(transfer: Transfer) -> None:
        if transfer.bytes_done > 0 and not transfer.status.finished:
            pair.receiver.cancel(transfer.id, "no thanks")

    pair.receiver._on_update = cancel_after_first_chunk
    sent = await pair.sender.send_file(src)

    assert sent.status is TransferStatus.CANCELLED
    assert sent.reason == "no thanks"
    assert sent.bytes_done < src.stat().st_size
    assert list(pair.downloads.iterdir()) == []


async def test_sender_times_out_without_confirmation(pair: Pair, tmp_path: Path):
    src = _make_file(tmp_path, "data.bin", 10)
    pair.sender._confirm_timeout = 0.1
    pair.receiver_channel.tamper = lambda data: None  # receiver's replies are lost

    sent = await pair.sender.send_file(src)

    assert sent.status is TransferStatus.FAILED
    assert "did not confirm" in sent.reason


async def test_peer_filename_cannot_escape_download_dir(pair: Pair, tmp_path: Path):
    meta = FileMetadataPayload(transfer_id=TID, filename="../../escape.txt", size=0)
    await pair.receiver.handle_message(make_message(MessageType.FILE_METADATA, meta))

    assert _received(pair).path == pair.downloads / "escape.txt"


async def test_out_of_order_chunk_fails_transfer(pair: Pair):
    meta = FileMetadataPayload(transfer_id=TID, filename="x.bin", size=100)
    await pair.receiver.handle_message(make_message(MessageType.FILE_METADATA, meta))

    pair.receiver.handle_frame(encode_chunk(TID, 50, b"x" * 10))

    assert _received(pair).status is TransferStatus.FAILED
    assert "expected offset 0" in _received(pair).reason


async def test_close_fails_active_transfers_and_removes_partial_file(pair: Pair):
    meta = FileMetadataPayload(transfer_id=TID, filename="x.bin", size=100)
    await pair.receiver.handle_message(make_message(MessageType.FILE_METADATA, meta))
    pair.receiver.handle_frame(encode_chunk(TID, 0, b"x" * 10))

    pair.receiver.close()

    assert _received(pair).status is TransferStatus.FAILED
    assert list(pair.downloads.iterdir()) == []


async def test_progress_updates_reach_completion(pair: Pair, tmp_path: Path):
    src = _make_file(tmp_path, "data.bin", 5 * CHUNK_SIZE)

    await pair.sender.send_file(src)

    assert pair.updates[0] == (TransferStatus.STREAMING, 0)
    assert pair.updates[-1] == (TransferStatus.COMPLETED, src.stat().st_size)
