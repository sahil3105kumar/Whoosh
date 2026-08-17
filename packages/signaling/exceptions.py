from __future__ import annotations


class SignalingError(Exception):
    """Base class for all signaling-layer errors."""


class RoomNotFoundError(SignalingError):
    """Raised when a room_id doesn't correspond to any known room."""

    def __init__(self, room_id: str) -> None:
        self.room_id = room_id
        super().__init__(f"No such room: {room_id!r}")


class RoomExpiredError(SignalingError):
    """Raised when a room_id existed but has since expired.

    Kept distinct from RoomNotFoundError so callers (and API
    responses) can tell "never existed" apart from "existed, timed
    out" — useful for a client that shows "this room has expired"
    instead of a generic 404.
    """

    def __init__(self, room_id: str) -> None:
        self.room_id = room_id
        super().__init__(f"Room expired: {room_id!r}")
