"""
Signaling-layer enumerations.

Distinct from packages/protocol/enums.py: MessageType there is the
*application* protocol carried on the DataChannel once peers are
connected. RoomState here is a signaling-only concept — it never
crosses the DataChannel and the RTC layer doesn't know it exists.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """Base class for string-valued enums (mirrors packages/protocol/enums.py)."""

    def __str__(self) -> str:
        return str(self.value)


class RoomState(StrEnum):
    """Lifecycle of a signaling room.



    CREATED is the instant a room exists but no peer has connected to
    it via WebSocket yet. The transition to WAITING happens when the creating peer's WebSocket
    connects.
    """

    CREATED = "created"
    WAITING = "waiting"
    CONNECTED = "connected"
    EXPIRED = "expired"
