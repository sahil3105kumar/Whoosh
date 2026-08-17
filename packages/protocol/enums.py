"""
Shared protocol enumerations.

These enums define the vocabulary used by every Whoosh client and the
signaling server.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):  # noqa: UP042
    """Base class for string-valued enums."""

    def __str__(self) -> str:
        return str(self.value)


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

    # Reserved for future versions
    TRANSFER_ACK = "transfer_ack"
    TRANSFER_RESUME = "transfer_resume"

    PEER_JOIN = "peer_join"
    PEER_LEAVE = "peer_leave"


class Capability(StrEnum):
    """Capabilities advertised during protocol negotiation."""

    CHAT = "chat"
    FILE_TRANSFER = "file_transfer"
    CANCEL = "cancel"

    # Future capabilities
    RESUME = "resume"
    MULTI_PEER = "multi_peer"
    ENCRYPTION = "encryption"
    COMPRESSION = "compression"


class TransferState(StrEnum):
    """Lifecycle of a file transfer."""

    PENDING = "pending"

    TRANSFERRING = "transferring"

    COMPLETED = "completed"

    CANCELLED = "cancelled"

    FAILED = "failed"


class ConnectionState(StrEnum):
    """High-level peer connection state."""

    CONNECTING = "connecting"

    CONNECTED = "connected"

    DISCONNECTED = "disconnected"

    FAILED = "failed"

    CLOSED = "closed"
