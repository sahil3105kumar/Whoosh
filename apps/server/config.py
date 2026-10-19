"""
Server configuration, read from ``WHOOSH_*`` environment variables.

Plain dataclass + os.environ rather than pydantic-settings: a handful
of scalar values doesn't justify another dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from packages.signaling.connection_manager import DEFAULT_RECONNECT_GRACE_SECONDS
from packages.signaling.room_manager import DEFAULT_ROOM_TTL_SECONDS


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    #: Seconds a room lives after creation.
    room_ttl_seconds: int = DEFAULT_ROOM_TTL_SECONDS
    #: Seconds a disconnected peer keeps its slot.
    reconnect_grace_seconds: float = DEFAULT_RECONNECT_GRACE_SECONDS
    #: Browser origins allowed to use the API. Empty = no restriction
    #: (fine for local dev; set it in production). Requests without an
    #: Origin header (CLI, curl) are always allowed — the check exists to
    #: stop other websites driving the server from a visitor's browser.
    allowed_origins: tuple[str, ...] = ()
    #: Rooms one client IP may create per minute.
    room_create_per_minute: int = 10
    #: People per room. Each pair holds a WebRTC connection (full mesh).
    max_peers: int = 8
    #: Largest signaling message accepted, in bytes. SDP is a few KB.
    max_message_bytes: int = 64 * 1024
    log_level: str = "INFO"
    #: STUN server URLs handed to clients via GET /config. Clients learn
    #: their public address from these; TURN would go here too (v4.0).
    stun_urls: tuple[str, ...] = ("stun:stun.l.google.com:19302",)
    #: Built web client (apps/web/dist) to serve at /. None = API only.
    web_dir: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        defaults = cls()
        return cls(
            room_ttl_seconds=int(env.get("WHOOSH_ROOM_TTL_SECONDS", defaults.room_ttl_seconds)),
            reconnect_grace_seconds=float(
                env.get("WHOOSH_RECONNECT_GRACE_SECONDS", defaults.reconnect_grace_seconds)
            ),
            allowed_origins=_csv(env.get("WHOOSH_ALLOWED_ORIGINS", "")),
            room_create_per_minute=int(
                env.get("WHOOSH_ROOM_CREATE_PER_MINUTE", defaults.room_create_per_minute)
            ),
            max_message_bytes=int(env.get("WHOOSH_MAX_MESSAGE_BYTES", defaults.max_message_bytes)),
            max_peers=int(env.get("WHOOSH_MAX_PEERS", defaults.max_peers)),
            log_level=env.get("WHOOSH_LOG_LEVEL", defaults.log_level).upper(),
            stun_urls=(
                _csv(env["WHOOSH_STUN_URLS"]) if "WHOOSH_STUN_URLS" in env else defaults.stun_urls
            ),
            web_dir=env.get("WHOOSH_WEB_DIR") or None,
        )

    def origin_allowed(self, origin: str | None) -> bool:
        return origin is None or not self.allowed_origins or origin in self.allowed_origins
