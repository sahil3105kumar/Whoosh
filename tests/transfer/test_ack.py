"""
Chunk acknowledgements (#18): receivers report what they've stored,
senders show acknowledged progress and keep a bounded window in flight.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.transfer.frames import CHUNK_SIZE
from packages.transfer.manager import ACK_EVERY, ACK_WINDOW, TransferStatus
from tests.transfer.test_resume import Session, no_report_throttle  # noqa: F401


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    return tmp_path / "downloads"


def _file(tmp_path: Path, size: int) -> Path:
    path = tmp_path / "data.bin"
    path.write_bytes(bytes(i % 251 for i in range(size)))
    return path


def _acks(sent: list[str | bytes]) -> list[int]:
    return [
        json.loads(m)["payload"]["offset"]
        for m in sent
        if isinstance(m, str) and json.loads(m)["type"] == "transfer_ack"
    ]


def _record(channel) -> list[str | bytes]:  # type: ignore[no-untyped-def]
    """Record everything sent on ``channel`` (and pass it through)."""
    seen: list[str | bytes] = []

    def tap(data: str | bytes) -> str | bytes:
        seen.append(data)
        return data

    channel.tamper = tap
    return seen


async def test_receiver_acks_every_mib_and_sender_tracks_them(downloads: Path, tmp_path: Path):
    size = 5 * ACK_EVERY + 123
    src = _file(tmp_path, size)
    session = Session(downloads)
    receiver_sent = _record(session.b)
    try:
        sent = await session.sender.send_file(src, ack=True)
    finally:
        await session.stop()

    assert sent.status is TransferStatus.COMPLETED
    acks = _acks(receiver_sent)
    assert acks == sorted(acks)
    assert len(acks) == 5  # one per full MiB; transfer_complete covers the tail
    assert all(b - a >= ACK_EVERY for a, b in zip([0, *acks], acks, strict=False))
    assert sent.bytes_acked == size


async def test_no_acks_unless_requested(downloads: Path, tmp_path: Path):
    src = _file(tmp_path, 3 * ACK_EVERY)
    session = Session(downloads)
    receiver_sent = _record(session.b)
    try:
        sent = await session.sender.send_file(src)
    finally:
        await session.stop()

    assert sent.status is TransferStatus.COMPLETED
    assert _acks(receiver_sent) == []


async def test_sender_stalls_when_acks_stop(downloads: Path, tmp_path: Path):
    src = _file(tmp_path, ACK_WINDOW + 4 * ACK_EVERY)
    session = Session(downloads)
    # The receiver keeps storing, but its acks never arrive
    session.b.tamper = lambda data: (
        None if isinstance(data, str) and '"transfer_ack"' in data else data
    )
    session.sender._stall_timeout = 0.2
    try:
        sent = await session.sender.send_file(src, ack=True)
    finally:
        await session.stop()

    assert sent.status is TransferStatus.FAILED
    assert sent.reason.startswith("stalled")
    assert sent.bytes_acked == 0
    assert ACK_WINDOW <= sent.bytes_done < ACK_WINDOW + CHUNK_SIZE  # the window held


@pytest.mark.usefixtures("no_report_throttle")
async def test_ack_outside_sent_range_is_ignored(downloads: Path, tmp_path: Path):
    src = _file(tmp_path, 3 * ACK_EVERY)
    session = Session(downloads)
    seen_acked: list[int] = []
    session.sender._on_update = lambda t: seen_acked.append(t.bytes_acked)

    def lie(data: str | bytes) -> str | bytes:
        if isinstance(data, str) and '"transfer_ack"' in data:
            msg = json.loads(data)
            msg["payload"]["offset"] = 10 * ACK_EVERY  # more than was ever sent
            return json.dumps(msg)
        return data

    session.b.tamper = lie
    try:
        sent = await session.sender.send_file(src, ack=True)
    finally:
        await session.stop()

    assert sent.status is TransferStatus.COMPLETED
    assert 10 * ACK_EVERY not in seen_acked
    assert max(seen_acked) == sent.size  # only the final confirmation completes it
