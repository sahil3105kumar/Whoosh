"""
Protocol codec.

Encodes ``ProtocolMessage`` instances to bytes and decodes raw bytes back
into validated ``ProtocolMessage`` objects.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from packages.protocol.constants import MAX_PROTOCOL_VERSION
from packages.protocol.enums import MessageType
from packages.protocol.exceptions import (
    MessageDecodeError,
    UnknownMessageTypeError,
    UnsupportedVersionError,
)
from packages.protocol.models import (
    ChatPayload,
    ErrorPayload,
    FileMetadataPayload,
    HelloPayload,
    Payload,
    PingPayload,
    PongPayload,
    ProgressPayload,
    ProtocolMessage,
    TransferCancelPayload,
    TransferCompletePayload,
)

# Maps every known MessageType to its payload model.
PAYLOAD_REGISTRY: dict[MessageType, type[Payload]] = {
    MessageType.HELLO: HelloPayload,
    MessageType.CHAT: ChatPayload,
    MessageType.FILE_METADATA: FileMetadataPayload,
    MessageType.PROGRESS: ProgressPayload,
    MessageType.TRANSFER_COMPLETE: TransferCompletePayload,
    MessageType.TRANSFER_CANCEL: TransferCancelPayload,
    MessageType.ERROR: ErrorPayload,
    MessageType.PING: PingPayload,
    MessageType.PONG: PongPayload,
}


def get_payload_model(message_type: MessageType) -> type[Payload] | None:
    """Return the payload model for *message_type*, or ``None`` if unregistered."""
    return PAYLOAD_REGISTRY.get(message_type)


def encode(message: ProtocolMessage) -> bytes:
    """Serialize a ``ProtocolMessage`` to UTF-8 JSON bytes."""
    return message.model_dump_json().encode("utf-8")


def decode(data: bytes) -> ProtocolMessage:
    """
    Deserialize raw bytes into a validated ``ProtocolMessage``.

    Raises
    ------
    MessageDecodeError
        If *data* is not valid JSON or cannot be parsed.
    UnsupportedVersionError
        If the message version exceeds ``MAX_PROTOCOL_VERSION``.
    UnknownMessageTypeError
        If the message type has no registered payload model.
    """
    # 1. Parse raw JSON
    try:
        raw = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MessageDecodeError(str(exc)) from exc

    if not isinstance(raw, dict):
        raise MessageDecodeError("Expected a JSON object")

    # 2. Version gate
    version = raw.get("version")
    if not isinstance(version, int):
        raise MessageDecodeError("Missing or non-integer 'version' field")
    if version > MAX_PROTOCOL_VERSION:
        raise UnsupportedVersionError(version, MAX_PROTOCOL_VERSION)

    # 3. Resolve message type
    raw_type = raw.get("type")
    if not isinstance(raw_type, str):
        raise UnknownMessageTypeError("<missing>" if raw_type is None else repr(raw_type))
    try:
        msg_type = MessageType(raw_type)
    except ValueError as err:
        raise UnknownMessageTypeError(raw_type or "<missing>") from err

    # 4. Ensure we have a registered payload model
    payload_model = get_payload_model(msg_type)
    if payload_model is None:
        raise UnknownMessageTypeError(str(msg_type))

    # 5. Validate the payload against the model registered for *this*
    #    type first. AnyPayload is an undiscriminated union and several
    #    payloads share a shape (ping/pong, complete/cancel), so letting
    #    the union pick would hand back the wrong class.
    try:
        payload = payload_model.model_validate(raw.get("payload"))
        return ProtocolMessage.model_validate({**raw, "payload": payload})
    except ValidationError as exc:
        raise MessageDecodeError(str(exc)) from exc
