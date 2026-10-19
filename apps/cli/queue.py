"""
Outgoing file queue for interactive mode: files are sent one at a time,
in the order they were added (#14). Mirrors apps/web/src/lib/queue.ts.

One at a time because concurrent sends would only split the same
DataChannel's bandwidth, finishing every file later than in sequence.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger(__name__)


class SendQueue:
    def __init__(self, send: Callable[[Path], Awaitable[object]]) -> None:
        self._send = send
        self._waiting: deque[Path] = deque()
        self._worker: asyncio.Task[None] | None = None
        #: The file being sent right now, if any.
        self.current: Path | None = None

    @property
    def waiting(self) -> list[Path]:
        """Files waiting to be sent (not including ``current``)."""
        return list(self._waiting)

    def add(self, paths: list[Path]) -> None:
        self._waiting.extend(paths)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    def clear(self) -> list[Path]:
        """Drop everything still waiting; returns what was dropped."""
        dropped = list(self._waiting)
        self._waiting.clear()
        return dropped

    async def close(self) -> None:
        """Drop the waiting files and stop the worker."""
        self.clear()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    async def _run(self) -> None:
        while self._waiting:
            self.current = self._waiting.popleft()
            try:
                await self._send(self.current)
            except Exception:
                # A failed send must not stall the rest of the queue
                log.exception("sending %s failed", self.current)
            finally:
                self.current = None
