"""
The Room domain entity.

A Room is pure data — it holds no logic of its own. RoomManager owns
all state transitions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .enums import RoomState
from .passwords import PasswordHash


@dataclass
class Room:
    """A signaling room.

    `peer_ids` only ever holds signaling-level peer identifiers. Room
    creation itself never populates it; a freshly created
    room always has zero peers.
    """

    id: str
    state: RoomState
    created_at: float
    expires_at: float
    peer_ids: set[str] = field(default_factory=set)
    #: Set when the creator chose a password (#28); only its hash is kept.
    password: PasswordHash | None = None

    @property
    def protected(self) -> bool:
        return self.password is not None

    def is_expired(self, *, now: float | None = None) -> bool:
        """Whether this room has passed its expiry time.

        Doesn't mutate `state` — RoomManager decides when/whether to
        transition a room to RoomState.EXPIRED. This just answers the
        yes/no question so RoomManager (and tests) don't duplicate
        the time comparison.
        """
        current = now if now is not None else time.time()
        return current >= self.expires_at
