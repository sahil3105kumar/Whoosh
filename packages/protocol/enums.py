"""
Shared protocol enumerations.

These enums define the vocabulary used by every Whoosh client and the
signaling server.
"""

from __future__ import annotations

from enum import StrEnum


class MessageType(StrEnum):
    """Application protocol message types."""

    HELLO = "hello"
    CHAT = "chat"
    FILE_METADATA = "file_metadata"
    FILE_CHUNK = "file_chunk"
    PROGRESS = "progress"
    TRANSFER_COMPLETE = "transfer_complete"
    TRANSFER_CANCEL = "transfer_cancel"
    ERROR = "error"
    PING = "ping"
    PONG = "pong"
    PEER_JOIN = "peer_join"
    PEER_LEAVE = "peer_leave"


class Capability(StrEnum):
    """Capabilities advertised during protocol negotiation."""

    CHAT = "chat"
    FILE_TRANSFER = "file_transfer"
    CANCEL = "cancel"
