"""Unit tests for apps/server/config.py."""

from __future__ import annotations

import pytest

from apps.server.config import Settings


def test_from_env_reads_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WHOOSH_ROOM_TTL_SECONDS", "120")
    monkeypatch.setenv("WHOOSH_ALLOWED_ORIGINS", "https://a.example, https://b.example,")
    monkeypatch.setenv("WHOOSH_LOG_LEVEL", "debug")

    settings = Settings.from_env()

    assert settings.room_ttl_seconds == 120
    assert settings.allowed_origins == ("https://a.example", "https://b.example")
    assert settings.log_level == "DEBUG"


def test_from_env_defaults(monkeypatch: pytest.MonkeyPatch):
    for name in ("WHOOSH_ROOM_TTL_SECONDS", "WHOOSH_ALLOWED_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_env().allowed_origins == ()


def test_origin_allowed():
    open_settings = Settings()
    locked = Settings(allowed_origins=("https://whoosh.example",))

    assert open_settings.origin_allowed("https://anything.example")
    assert locked.origin_allowed("https://whoosh.example")
    assert locked.origin_allowed(None)  # non-browser clients send no Origin
    assert not locked.origin_allowed("https://evil.example")


def test_stun_urls_can_be_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WHOOSH_STUN_URLS", "")
    assert Settings.from_env().stun_urls == ()
