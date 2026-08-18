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
from packages.protocol.models import ProtocolMessage
from packages.protocol.registry import get_payload_model


class ProtocolCodec:
    """Stateless encoder / decoder for the Whoosh application protocol."""

    # -- Encoding ------------------------------------------------------------

    @staticmethod
    # static method is used when we don't need to use self. It can be called
    # without creating an instance of the class. This method belongs to the
    # class, but it does not need access to the object (self) or the class (cls).
    def encode(message: ProtocolMessage) -> bytes:
        """Serialize a ``ProtocolMessage`` to UTF-8 JSON bytes."""
        return message.model_dump_json().encode("utf-8")

    # -- Decoding ------------------------------------------------------------

    @staticmethod
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
        try:
            msg_type = MessageType(raw_type)
        except ValueError as err:
            raise UnknownMessageTypeError(raw_type or "<missing>") from err

        # 4. Ensure we have a registered payload model
        if get_payload_model(msg_type) is None:
            raise UnknownMessageTypeError(str(msg_type))

        # 5. Full Pydantic validation
        try:
            return ProtocolMessage.model_validate(raw)
        except ValidationError as exc:
            raise MessageDecodeError(str(exc)) from exc
