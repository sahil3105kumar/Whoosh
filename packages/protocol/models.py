"""
Pydantic models for the shared application protocol.

Every message sent over the WebRTC DataChannel uses the ``ProtocolMessage``
envelope wrapping a typed payload.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import BaseModel, Field

from packages.protocol.constants import PROTOCOL_VERSION
from packages.protocol.enums import Capability, MessageType

# ---------------------------------------------------------------------------
# Payload models
# ---------------------------------------------------------------------------


class Payload(BaseModel):
    """Base class for all payload models."""


class HelloPayload(Payload):
    """Capability negotiation sent immediately after the DataChannel opens."""

    capabilities: list[Capability]


class ChatPayload(Payload):
    """A text chat message."""

    text: str


class FileMetadataPayload(Payload):
    """Describes a file about to be transferred."""

    transfer_id: str
    filename: str
    size: int
    mime_type: str = "application/octet-stream"


class FileChunkPayload(Payload):
    """A single chunk of a file transfer (base64-encoded for JSON transport)."""

    transfer_id: str
    chunk_index: int
    data: str  # base64-encoded binary data


class ProgressPayload(Payload):
    """Transfer progress update."""

    transfer_id: str
    bytes_transferred: int
    total_bytes: int


class TransferCompletePayload(Payload):
    """Signals that a file transfer finished successfully."""

    transfer_id: str


class TransferCancelPayload(Payload):
    """Cancels an active file transfer."""

    transfer_id: str
    reason: str = ""


class ErrorPayload(Payload):
    """Reports a protocol or transfer error."""

    code: str
    message: str


class PingPayload(Payload):
    """Latency measurement request."""

    timestamp: float


class PongPayload(Payload):
    """Latency measurement response."""

    timestamp: float


class PeerJoinPayload(Payload):
    """Notification that a peer joined the room."""

    peer_id: str


class PeerLeavePayload(Payload):
    """Notification that a peer left the room."""

    peer_id: str


# ---------------------------------------------------------------------------
# Discriminated union of all payload types
# ---------------------------------------------------------------------------

AnyPayload = Annotated[
    (
        HelloPayload
        | ChatPayload
        | FileMetadataPayload
        | FileChunkPayload
        | ProgressPayload
        | TransferCompletePayload
        | TransferCancelPayload
        | ErrorPayload
        | PingPayload
        | PongPayload
        | PeerJoinPayload
        | PeerLeavePayload
    ),
    Field(discriminator=None),
]


# ---------------------------------------------------------------------------
# Protocol envelope
# ---------------------------------------------------------------------------


class ProtocolMessage(BaseModel):
    """
    Top-level envelope for every application-protocol message.

    ``version`` is always set to the current protocol version on creation.
    ``id`` is auto-generated if not provided.
    ``type`` indicates how to interpret ``payload``.
    """

    version: int = PROTOCOL_VERSION
    type: MessageType
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    payload: AnyPayload


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def make_message(msg_type: MessageType, payload: Payload) -> ProtocolMessage:
    """Build a ``ProtocolMessage`` with auto-generated id and current version."""
    return ProtocolMessage(type=msg_type, payload=payload)
