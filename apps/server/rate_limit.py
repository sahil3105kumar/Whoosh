"""
In-memory per-key token bucket.

Single-process only, like the rest of the server's state. Behind a
reverse proxy, run uvicorn with ``--proxy-headers`` so the key is the
real client IP rather than the proxy's.
"""

from __future__ import annotations

import time


class RateLimiter:
    """Allows ``per_minute`` hits per key, refilling continuously."""

    def __init__(self, per_minute: int) -> None:
        self._capacity = float(per_minute)
        self._refill_per_second = per_minute / 60.0
        # key -> (tokens, last_update)
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Consume one token for ``key``; False if none are left."""
        current = now if now is not None else time.monotonic()
        tokens, last = self._buckets.get(key, (self._capacity, current))
        tokens = min(self._capacity, tokens + (current - last) * self._refill_per_second)
        if tokens < 1:
            self._buckets[key] = (tokens, current)
            return False
        self._buckets[key] = (tokens - 1, current)
        return True

    def prune(self, *, now: float | None = None) -> None:
        """Forget keys whose bucket has refilled, so the dict doesn't
        grow with every IP ever seen."""
        current = now if now is not None else time.monotonic()
        full_after = self._capacity / self._refill_per_second if self._refill_per_second else 0
        self._buckets = {
            key: (tokens, last)
            for key, (tokens, last) in self._buckets.items()
            if current - last < full_after
        }

    def reset(self) -> None:
        self._buckets.clear()
