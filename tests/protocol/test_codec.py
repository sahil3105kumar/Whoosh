"""Tests for the protocol codec, models, and registry."""

from __future__ import annotations

import json
import time
import uuid

import pytest

from packages.protocol.codec import ProtocolCodec
from packages.protocol.constants import MAX_PROTOCOL_VERSION, PROTOCOL_VERSION
from packages.protocol.enums import Capability, MessageType
from packages.protocol.exceptions import (
    MessageDecodeError,
    UnknownMessageTypeError,
    UnsupportedVersionError,
)
from packages.protocol.models import (
    ChatPayload,
    ErrorPayload,
    FileChunkPayload,
    FileMetadataPayload,
    HelloPayload,
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
from packages.protocol.registry import get_payload_model

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

codec = ProtocolCodec()


def _roundtrip(msg: ProtocolMessage) -> ProtocolMessage:
    """Encode then decode, returning the decoded message."""
    return codec.decode(codec.encode(msg))


# ---------------------------------------------------------------------------
# Encode / decode round-trips for every message type
# ---------------------------------------------------------------------------


class TestRoundTrips:
    def test_hello(self):
        original = make_message(
            MessageType.HELLO,
            HelloPayload(capabilities=[Capability.CHAT, Capability.FILE_TRANSFER]),
        )
        decoded = _roundtrip(original)
        assert decoded.type == MessageType.HELLO
        assert decoded.payload.capabilities == [Capability.CHAT, Capability.FILE_TRANSFER]

    def test_chat(self):
        original = make_message(MessageType.CHAT, ChatPayload(text="hello world"))
        decoded = _roundtrip(original)
        assert decoded.type == MessageType.CHAT
        assert decoded.payload.text == "hello world"

    def test_file_metadata(self):
        original = make_message(
            MessageType.FILE_METADATA,
            FileMetadataPayload(
                transfer_id="t1", filename="notes.txt", size=1024, mime_type="text/plain"
            ),
        )
        decoded = _roundtrip(original)
        assert decoded.payload.filename == "notes.txt"
        assert decoded.payload.size == 1024

    def test_file_chunk(self):
        original = make_message(
            MessageType.FILE_CHUNK,
            FileChunkPayload(transfer_id="t1", chunk_index=0, data="AQIDBA=="),
        )
        decoded = _roundtrip(original)
        assert decoded.payload.chunk_index == 0
        assert decoded.payload.data == "AQIDBA=="

    def test_progress(self):
        original = make_message(
            MessageType.PROGRESS,
            ProgressPayload(transfer_id="t1", bytes_transferred=512, total_bytes=1024),
        )
        decoded = _roundtrip(original)
        assert decoded.payload.bytes_transferred == 512

    def test_transfer_complete(self):
        original = make_message(
            MessageType.TRANSFER_COMPLETE,
            TransferCompletePayload(transfer_id="t1"),
        )
        decoded = _roundtrip(original)
        assert decoded.type == MessageType.TRANSFER_COMPLETE

    def test_transfer_cancel(self):
        original = make_message(
            MessageType.TRANSFER_CANCEL,
            TransferCancelPayload(transfer_id="t1", reason="user cancelled"),
        )
        decoded = _roundtrip(original)
        assert decoded.payload.reason == "user cancelled"

    def test_error(self):
        original = make_message(
            MessageType.ERROR,
            ErrorPayload(code="E001", message="something went wrong"),
        )
        decoded = _roundtrip(original)
        assert decoded.payload.code == "E001"

    def test_ping(self):
        ts = time.time()
        original = make_message(MessageType.PING, PingPayload(timestamp=ts))
        decoded = _roundtrip(original)
        assert decoded.payload.timestamp == ts

    def test_pong(self):
        ts = time.time()
        original = make_message(MessageType.PONG, PongPayload(timestamp=ts))
        decoded = _roundtrip(original)
        assert decoded.payload.timestamp == ts

    def test_peer_join(self):
        original = make_message(MessageType.PEER_JOIN, PeerJoinPayload(peer_id="abc"))
        decoded = _roundtrip(original)
        assert decoded.payload.peer_id == "abc"

    def test_peer_leave(self):
        original = make_message(MessageType.PEER_LEAVE, PeerLeavePayload(peer_id="abc"))
        decoded = _roundtrip(original)
        assert decoded.payload.peer_id == "abc"


# ---------------------------------------------------------------------------
# Envelope defaults
# ---------------------------------------------------------------------------


class TestEnvelopeDefaults:
    def test_version_is_current(self):
        msg = make_message(MessageType.CHAT, ChatPayload(text="hi"))
        assert msg.version == PROTOCOL_VERSION

    def test_id_is_valid_uuid(self):
        msg = make_message(MessageType.CHAT, ChatPayload(text="hi"))
        uuid.UUID(msg.id)  # raises if invalid

    def test_encode_returns_bytes(self):
        msg = make_message(MessageType.CHAT, ChatPayload(text="hi"))
        encoded = codec.encode(msg)
        assert isinstance(encoded, bytes)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestDecodeErrors:
    def test_invalid_json(self):
        with pytest.raises(MessageDecodeError):
            codec.decode(b"not json")

    def test_non_object_json(self):
        with pytest.raises(MessageDecodeError, match="Expected a JSON object"):
            codec.decode(b'"just a string"')

    def test_missing_version(self):
        raw = json.dumps({"type": "chat", "id": "x", "payload": {"text": "hi"}}).encode()
        with pytest.raises(MessageDecodeError, match="version"):
            codec.decode(raw)

    def test_unsupported_version(self):
        raw = json.dumps(
            {
                "version": MAX_PROTOCOL_VERSION + 1,
                "type": "chat",
                "id": "x",
                "payload": {"text": "hi"},
            }
        ).encode()
        with pytest.raises(UnsupportedVersionError):
            codec.decode(raw)

    def test_unknown_message_type(self):
        raw = json.dumps({"version": 1, "type": "nonexistent", "id": "x", "payload": {}}).encode()
        with pytest.raises(UnknownMessageTypeError):
            codec.decode(raw)

    def test_missing_type(self):
        raw = json.dumps({"version": 1, "id": "x", "payload": {}}).encode()
        with pytest.raises(UnknownMessageTypeError):
            codec.decode(raw)

    def test_invalid_payload(self):
        raw = json.dumps(
            {"version": 1, "type": "file_metadata", "id": "x", "payload": {"bad": "data"}}
        ).encode()
        with pytest.raises(MessageDecodeError):
            codec.decode(raw)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_all_message_types_registered(self):
        # Every non-reserved MessageType should have a payload model
        registered = {
            MessageType.HELLO,
            MessageType.CHAT,
            MessageType.FILE_METADATA,
            MessageType.FILE_CHUNK,
            MessageType.PROGRESS,
            MessageType.TRANSFER_COMPLETE,
            MessageType.TRANSFER_CANCEL,
            MessageType.ERROR,
            MessageType.PING,
            MessageType.PONG,
            MessageType.PEER_JOIN,
            MessageType.PEER_LEAVE,
        }
        for mt in registered:
            assert get_payload_model(mt) is not None, f"{mt} not registered"

    def test_unknown_type_returns_none(self):
        # Reserved types that are not yet registered
        assert get_payload_model(MessageType.TRANSFER_ACK) is None
        assert get_payload_model(MessageType.TRANSFER_RESUME) is None
