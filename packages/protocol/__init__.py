"""
Whoosh shared application protocol.

Public API for encoding, decoding, and constructing protocol messages.
"""

from packages.protocol.codec import ProtocolCodec
from packages.protocol.constants import (
    DEFAULT_CHUNK_SIZE,
    MAX_CHAT_MESSAGE_LENGTH,
    MAX_CHUNK_SIZE,
    MAX_FILENAME_LENGTH,
    MAX_PROTOCOL_VERSION,
    PEER_TIMEOUT,
    PING_INTERVAL,
    PROTOCOL_VERSION,
)
from packages.protocol.enums import (
    Capability,
    ConnectionState,
    MessageType,
    TransferState,
)
from packages.protocol.exceptions import (
    MessageDecodeError,
    ProtocolError,
    UnknownMessageTypeError,
    UnsupportedVersionError,
)
from packages.protocol.models import (
    AnyPayload,
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
    ProtocolMessage,
    TransferCancelPayload,
    TransferCompletePayload,
    make_message,
)
from packages.protocol.registry import PAYLOAD_REGISTRY, get_payload_model

__all__ = [
    # Codec
    "ProtocolCodec",
    # Constants
    "DEFAULT_CHUNK_SIZE",
    "MAX_CHAT_MESSAGE_LENGTH",
    "MAX_CHUNK_SIZE",
    "MAX_FILENAME_LENGTH",
    "MAX_PROTOCOL_VERSION",
    "PEER_TIMEOUT",
    "PING_INTERVAL",
    "PROTOCOL_VERSION",
    # Enums
    "Capability",
    "ConnectionState",
    "MessageType",
    "TransferState",
    # Exceptions
    "MessageDecodeError",
    "ProtocolError",
    "UnknownMessageTypeError",
    "UnsupportedVersionError",
    # Models
    "AnyPayload",
    "ChatPayload",
    "ErrorPayload",
    "FileChunkPayload",
    "FileMetadataPayload",
    "HelloPayload",
    "Payload",
    "PeerJoinPayload",
    "PeerLeavePayload",
    "PingPayload",
    "PongPayload",
    "ProgressPayload",
    "ProtocolMessage",
    "TransferCancelPayload",
    "TransferCompletePayload",
    "make_message",
    # Registry
    "PAYLOAD_REGISTRY",
    "get_payload_model",
]
