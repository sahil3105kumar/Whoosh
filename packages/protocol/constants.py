"""
Shared protocol constants.

These values define the wire protocol and must remain stable across all
Whoosh clients (Web, CLI) and the signaling server.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

#: Current protocol version.
PROTOCOL_VERSION: int = 1

#: Maximum supported protocol version.
MAX_PROTOCOL_VERSION: int = 1

# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------

#: Default file chunk size (256 KiB).
DEFAULT_CHUNK_SIZE: int = 256 * 1024

#: Maximum allowed chunk size (1 MiB).
MAX_CHUNK_SIZE: int = 1024 * 1024

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

#: Seconds between ping messages.
PING_INTERVAL: int = 10

#: Seconds before considering a peer unreachable.
PEER_TIMEOUT: int = 30

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

#: Maximum length of a chat message.
MAX_CHAT_MESSAGE_LENGTH: int = 4096

#: Maximum length of a filename.
MAX_FILENAME_LENGTH: int = 255
