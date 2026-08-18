from __future__ import annotations

import secrets
import time

from .enums import RoomState
from .exceptions import RoomExpiredError, RoomFullError, RoomNotFoundError
from .models import Room

#: How long a room lives before it's considered expired if nobody's acted on it.
#: Chosen to comfortably cover "create a room, share the code, other person joins"
#: without leaving stale rooms around for long — tune once real usage data exists.
DEFAULT_ROOM_TTL_SECONDS: int = 10 * 60

#: Room IDs are meant to be typed or read aloud/shared quickly, not cryptographically
#: unguessable — that's why they're short rather than full UUIDs. 4 bytes (8 hex
#: chars) is ~4 billion possibilities, reasonable for a short-lived, low-value target.
#: If rooms ever hold sensitive data before v4.0's password-protected rooms lands, this
#: is the first thing to revisit.
_ROOM_ID_BYTES = 4

#: Maximum number of peers allowed in a room (v1 is strictly 1-to-1).
_MAX_PEERS = 2


def _generate_room_id() -> str:
    return secrets.token_hex(_ROOM_ID_BYTES)


class RoomManager:
    """In-memory registry of active signaling rooms."""

    def __init__(self, *, ttl_seconds: int = DEFAULT_ROOM_TTL_SECONDS) -> None:
        self._rooms: dict[str, Room] = {}
        self._ttl_seconds = ttl_seconds

    def create_room(self) -> Room:
        """Create a new room in RoomState.CREATED and register it.

        Retries on the (astronomically unlikely) chance of a room_id
        collision rather than assuming uniqueness.
        """
        now = time.time()
        room_id = _generate_room_id()
        while room_id in self._rooms:
            room_id = _generate_room_id()

        room = Room(
            id=room_id,
            state=RoomState.CREATED,
            created_at=now,
            expires_at=now + self._ttl_seconds,
        )
        self._rooms[room_id] = room
        return room

    def get_room(self, room_id: str) -> Room:
        """Look up a room by id.

        Raises RoomNotFoundError if the id is unknown, or
        RoomExpiredError if it existed but has timed out — an
        expired room is also evicted from storage as a side effect,
        so it won't be found on a subsequent call either.
        """
        room = self._rooms.get(room_id)
        if room is None:
            raise RoomNotFoundError(room_id)

        if room.is_expired():
            del self._rooms[room_id]
            raise RoomExpiredError(room_id)

        return room

    def join_room(self, room_id: str, peer_id: str) -> Room:
        """Add a peer to a room.

        Transitions room state: CREATED -> WAITING (1 peer),
        WAITING -> CONNECTED (2 peers). Raises RoomFullError if
        the room already has the maximum number of peers.
        """
        room = self.get_room(room_id)

        if len(room.peer_ids) >= _MAX_PEERS:
            raise RoomFullError(room_id)

        room.peer_ids.add(peer_id)

        if len(room.peer_ids) == 1:
            room.state = RoomState.WAITING
        elif len(room.peer_ids) == 2:
            room.state = RoomState.CONNECTED

        return room

    def leave_room(self, room_id: str, peer_id: str) -> None:
        """Remove a peer from a room.

        The room stays open when one peer leaves — only when all peers
        have left (room is empty) does it get expired and evicted.
        State transitions back to WAITING if one peer remains.
        """
        try:
            room = self.get_room(room_id)
        except (RoomNotFoundError, RoomExpiredError):
            return  # Already gone, nothing to do

        room.peer_ids.discard(peer_id)

        if len(room.peer_ids) == 0:
            room.state = RoomState.EXPIRED
            del self._rooms[room_id]
        elif len(room.peer_ids) == 1:
            room.state = RoomState.WAITING

    def room_count(self) -> int:
        """Number of rooms currently tracked (including any not yet
        lazily evicted for expiry). Mainly useful for tests and
        health/debug endpoints.
        """
        return len(self._rooms)
