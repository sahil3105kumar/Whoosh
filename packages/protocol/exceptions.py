"""
Protocol-specific exceptions.

Raised during message encoding, decoding, and validation.
"""

from __future__ import annotations


class ProtocolError(Exception):
    """Base exception for all protocol-related errors."""


class UnsupportedVersionError(ProtocolError):
    """Raised when a message's protocol version is not supported."""

    def __init__(self, version: int, max_version: int) -> None:
        self.version = version
        self.max_version = max_version
        super().__init__(f"Unsupported protocol version {version} (max supported: {max_version})")


class UnknownMessageTypeError(ProtocolError):
    """Raised when a message type has no registered payload model."""

    def __init__(self, message_type: str) -> None:
        self.message_type = message_type
        super().__init__(f"Unknown message type: {message_type!r}")


class MessageDecodeError(ProtocolError):
    """Raised when a message cannot be decoded from raw bytes."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Failed to decode message: {reason}")
