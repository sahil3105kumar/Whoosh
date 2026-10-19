"""
Shared protocol enumerations.

These enums define the vocabulary used by every Whoosh client and the
signaling server.
"""

from __future__ import annotations

from enum import StrEnum


class MessageType(StrEnum):
    """Application protocol message types (JSON text frames).

    File data is not a message type: chunks travel as binary frames,
    see packages/transfer/frames.py.
    """

    HELLO = "hello"
    CHAT = "chat"
    FILE_METADATA = "file_metadata"
    PROGRESS = "progress"
    TRANSFER_COMPLETE = "transfer_complete"
    TRANSFER_CANCEL = "transfer_cancel"
    TRANSFER_ACCEPT = "transfer_accept"
    ERROR = "error"
    PING = "ping"
    PONG = "pong"


class Capability(StrEnum):
    """Capabilities advertised during protocol negotiation."""

    CHAT = "chat"
    FILE_TRANSFER = "file_transfer"
    CANCEL = "cancel"
    #: Interrupted transfers continue from where they stopped (resume_key
    #: in file_metadata, transfer_accept with an offset).
    RESUME = "resume"
