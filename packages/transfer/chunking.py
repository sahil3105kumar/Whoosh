"""
Adaptive chunk sizing (#21, docs/decisions.md D52).

Senders start with small chunks and grow them while throughput keeps
improving: per-message overhead (framing, hashing, aiortc's Python SCTP
stack) makes small chunks slow on fast links, while large ones help
nothing once the network is the bottleneck. Frames carry byte offsets,
so receivers accept any size and need no negotiation; the only limit is
the connection's maximum message size.
"""

from __future__ import annotations

from packages.transfer.frames import CHUNK_SIZE, HEADER_SIZE

#: Smallest and largest chunk (file bytes per frame).
MIN_CHUNK = CHUNK_SIZE
MAX_CHUNK = 256 * 1024

#: Throughput is measured over windows of at least this long and this many bytes.
WINDOW_SECONDS = 0.25
WINDOW_BYTES = 512 * 1024

#: Grow while a window is this much faster than the previous one; shrink
#: when it's this much slower. In between, hold (noise, or a plateau).
GROW_IF_FASTER = 1.05
SHRINK_IF_SLOWER = 0.8


class ChunkSizer:
    def __init__(self, max_message_size: int) -> None:
        self.max_chunk = max(MIN_CHUNK, min(MAX_CHUNK, max_message_size - HEADER_SIZE))
        self.size = MIN_CHUNK
        self._window_start: float | None = None
        self._window_bytes = 0
        self._last_rate: float | None = None

    def sent(self, nbytes: int, now: float) -> None:
        """Record ``nbytes`` sent at time ``now``; may change ``size``."""
        if self._window_start is None:
            # Open the first window; these bytes left before it, don't count them
            self._window_start = now
            return
        self._window_bytes += nbytes
        elapsed = now - self._window_start
        if elapsed < WINDOW_SECONDS or self._window_bytes < WINDOW_BYTES:
            return
        self._adjust(self._window_bytes / elapsed)
        self._window_start = now
        self._window_bytes = 0

    def _adjust(self, rate: float) -> None:
        last = self._last_rate
        self._last_rate = rate
        if last is None or rate >= last * GROW_IF_FASTER:
            self.size = min(self.size * 2, self.max_chunk)
        elif rate < last * SHRINK_IF_SLOWER:
            self.size = max(self.size // 2, MIN_CHUNK)
