"""Unit tests for apps/server/rate_limit.py and the POST /rooms limit."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.server import main
from apps.server.rate_limit import RateLimiter


def test_allows_up_to_capacity_then_denies():
    limiter = RateLimiter(per_minute=3)
    assert [limiter.allow("ip", now=0) for _ in range(4)] == [True, True, True, False]


def test_keys_are_independent():
    limiter = RateLimiter(per_minute=1)
    assert limiter.allow("a", now=0)
    assert limiter.allow("b", now=0)
    assert not limiter.allow("a", now=0)


def test_refills_over_time():
    limiter = RateLimiter(per_minute=60)  # one token per second
    for _ in range(60):
        limiter.allow("ip", now=0)
    assert not limiter.allow("ip", now=0.5)
    assert limiter.allow("ip", now=1.5)


def test_prune_forgets_refilled_buckets_only():
    limiter = RateLimiter(per_minute=60)
    limiter.allow("old", now=0)
    limiter.allow("recent", now=59)

    limiter.prune(now=61)

    assert set(limiter._buckets) == {"recent"}


def test_create_room_returns_429_when_limited(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "room_create_limiter", RateLimiter(per_minute=2))
    client = TestClient(main.app)

    statuses = [client.post("/rooms").status_code for _ in range(3)]

    assert statuses == [201, 201, 429]
