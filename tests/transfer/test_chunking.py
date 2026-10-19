"""Adaptive chunk sizing (#21)."""

from __future__ import annotations

from collections.abc import Callable

from packages.transfer.chunking import MAX_CHUNK, MIN_CHUNK, WINDOW_SECONDS, ChunkSizer
from packages.transfer.frames import HEADER_SIZE


def _run(sizer: ChunkSizer, rate_for: Callable[[int], float], windows: int = 40) -> list[int]:
    """Feed the sizer windows at the throughput ``rate_for(chunk size)``."""
    now, sizes = 0.0, []
    for _ in range(windows):
        rate = rate_for(sizer.size)
        nbytes = int(rate * WINDOW_SECONDS * 1.01)
        now += nbytes / rate
        sizer.sent(nbytes, now)
        sizes.append(sizer.size)
    return sizes


def test_starts_small():
    assert ChunkSizer(65536).size == MIN_CHUNK


def test_grows_while_bigger_chunks_are_faster():
    # Per-message overhead dominates: throughput rises with chunk size
    sizes = _run(ChunkSizer(1 << 20), lambda size: 1e6 * size / (size + 20_000))
    assert sizes[-1] == MAX_CHUNK


def test_holds_on_a_plateau():
    # Network-bound: anything above 32 KiB is no faster
    sizes = _run(ChunkSizer(1 << 20), lambda size: 1e6 * min(size, 32 * 1024))
    assert sizes[-1] == 64 * 1024  # one probe past the plateau, then holds
    assert len(set(sizes[-10:])) == 1


def test_shrinks_when_throughput_drops():
    sizer = ChunkSizer(1 << 20)
    sizer.size = 128 * 1024
    sizer._last_rate = 10e6
    sizer.sent(10_000_000, 0.0)  # starts a window
    sizer.sent(1_000_000, 1.0)  # 1 MB/s: much slower than before
    assert sizer.size == 64 * 1024


def test_never_exceeds_the_connection_limit():
    sizer = ChunkSizer(65536)
    sizes = _run(sizer, lambda size: 1e6 * size)
    assert max(sizes) == 65536 - HEADER_SIZE
    assert sizer.max_chunk + HEADER_SIZE <= 65536


def test_tiny_limits_still_send_minimum_chunks():
    assert ChunkSizer(1000).max_chunk == MIN_CHUNK
