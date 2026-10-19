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
    TRANSFER_COMPLETE = "transfer_complete"
    TRANSFER_CANCEL = "transfer_cancel"
    TRANSFER_ACCEPT = "transfer_accept"
    TRANSFER_ACK = "transfer_ack"
    ERROR = "error"
    PING = "ping"
    PONG = "pong"
    BYE = "bye"
    KEY_EXCHANGE = "key_exchange"


class Capability(StrEnum):
    """Capabilities advertised during protocol negotiation."""

    CHAT = "chat"
    FILE_TRANSFER = "file_transfer"
    CANCEL = "cancel"
    #: Interrupted transfers continue from where they stopped (resume_key
    #: in file_metadata, transfer_accept with an offset).
    RESUME = "resume"
    #: Receivers acknowledge what they've stored (transfer_ack); senders
    #: show acknowledged progress and cap unacknowledged data in flight.
    ACK = "ack"
    #: End-to-end encryption (D60). Required: peers without it are refused,
    #: so stripping it from hello can't downgrade a session to plaintext.
    E2E = "e2e"
