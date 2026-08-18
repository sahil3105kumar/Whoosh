"""
Message registry.

Maps ``MessageType`` values to their corresponding Pydantic payload model,
allowing the codec to resolve the correct type during deserialization.
"""

from __future__ import annotations

from packages.protocol.enums import MessageType
from packages.protocol.models import (
    ChatPayload,
    ErrorPayload,
    FileChunkPayload,
    FileMetadataPayload,
    HelloPayload,
    Payload,
    PeerJoinPayload,
    PeerLeavePayload,
    PingPayload,
    PongPayload,
    ProgressPayload,
    TransferCancelPayload,
    TransferCompletePayload,
)

# Maps every known MessageType to its payload model.
PAYLOAD_REGISTRY: dict[MessageType, type[Payload]] = {
    MessageType.HELLO: HelloPayload,
    MessageType.CHAT: ChatPayload,
    MessageType.FILE_METADATA: FileMetadataPayload,
    MessageType.FILE_CHUNK: FileChunkPayload,
    MessageType.PROGRESS: ProgressPayload,
    MessageType.TRANSFER_COMPLETE: TransferCompletePayload,
    MessageType.TRANSFER_CANCEL: TransferCancelPayload,
    MessageType.ERROR: ErrorPayload,
    MessageType.PING: PingPayload,
    MessageType.PONG: PongPayload,
    MessageType.PEER_JOIN: PeerJoinPayload,
    MessageType.PEER_LEAVE: PeerLeavePayload,
}


def get_payload_model(message_type: MessageType) -> type[Payload] | None:
    """Return the payload model for *message_type*, or ``None`` if unregistered."""
    return PAYLOAD_REGISTRY.get(message_type)
