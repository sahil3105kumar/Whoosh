"""
Shared protocol constants.

These values define the wire protocol and must remain stable across all
Whoosh clients (Web, CLI) and the signaling server.
"""

from __future__ import annotations

#: Current protocol version.
PROTOCOL_VERSION: int = 1

#: Maximum supported protocol version.
MAX_PROTOCOL_VERSION: int = 1
