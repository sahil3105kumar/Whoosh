"""
WebSocket connection manager for signaling rooms.

Tracks which peers have active WebSocket connections and handles
message relay. Peers remain in the room after their WebSocket
disconnects so they can reconnect; if they don't come back within
the grace period, their slot is released.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import WebSocket

    from .room_manager import RoomManager

logger = logging.getLogger(__name__)

#: How long a disconnected peer keeps its room slot before it's released.
#: Long enough to ride out a network blip or a laptop lid close/open.
DEFAULT_RECONNECT_GRACE_SECONDS: float = 30.0


class ConnectionManager:
    """Maps room_id -> {peer_id -> WebSocket} for active connections."""

    def __init__(
        self,
        room_manager: RoomManager,
        *,
        reconnect_grace_seconds: float = DEFAULT_RECONNECT_GRACE_SECONDS,
    ) -> None:
        self._room_manager = room_manager
        self._grace_seconds = reconnect_grace_seconds
        # room_id -> {peer_id -> WebSocket}
        self._connections: dict[str, dict[str, WebSocket]] = {}
        # (room_id, peer_id) -> pending slot-release task
        self._release_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def connect(
        self,
        room_id: str,
        websocket: WebSocket,
        peer_id: str | None = None,
    ) -> str:
        """Register a WebSocket for a peer in a room.

        If ``peer_id`` is None, a new one is generated and the peer is
        joined to the room via RoomManager. If ``peer_id`` is provided
        and already belongs to the room, this is treated as a
        reconnection — the WebSocket reference is updated without
        calling join_room again, and any pending slot release is
        cancelled.

        Returns the peer_id (generated or reused).

        Raises RoomNotFoundError, RoomExpiredError, or RoomFullError
        on failure — callers should catch and close the WebSocket.
        """
        room = self._room_manager.get_room(room_id)

        if peer_id is not None and peer_id in room.peer_ids:
            # Reconnection: peer already has a slot, just update the socket
            release = self._release_tasks.pop((room_id, peer_id), None)
            if release is not None:
                release.cancel()
        else:
            # New peer — join_room handles the full/expired checks
            if peer_id is None:
                peer_id = uuid.uuid4().hex
            self._room_manager.join_room(room_id, peer_id)

        # Register the WebSocket
        if room_id not in self._connections:
            self._connections[room_id] = {}
        self._connections[room_id][peer_id] = websocket

        # Notify other connected peers
        await self.relay(room_id, peer_id, {"type": "peer_joined", "peer_id": peer_id})

        return peer_id

    async def disconnect(self, room_id: str, peer_id: str, websocket: WebSocket) -> None:
        """Remove the WebSocket reference without leaving the room.

        Only removes the entry if it still points at ``websocket``: if
        the peer already reconnected on a new socket, the stale
        socket's cleanup must not unregister the live one.

        The peer's slot is preserved for reconnection and released
        after the grace period. Notifies the remaining peer that
        signaling dropped.
        """
        room_peers = self._connections.get(room_id)
        if room_peers is None or room_peers.get(peer_id) is not websocket:
            return

        del room_peers[peer_id]

        # Notify remaining connected peers
        await self.relay(room_id, peer_id, {"type": "peer_disconnected", "peer_id": peer_id})

        # Clean up the dict entry if no active connections remain
        if not room_peers:
            del self._connections[room_id]

        key = (room_id, peer_id)
        self._release_tasks[key] = asyncio.create_task(self._release_after_grace(*key))

    async def _release_after_grace(self, room_id: str, peer_id: str) -> None:
        await asyncio.sleep(self._grace_seconds)
        self._release_tasks.pop((room_id, peer_id), None)
        if self.is_connected(room_id, peer_id):
            return
        self._room_manager.leave_room(room_id, peer_id)
        await self.relay(room_id, peer_id, {"type": "peer_left", "peer_id": peer_id})

    async def relay(self, room_id: str, sender_id: str, message: dict[str, Any]) -> None:
        """Send a JSON message to every connected peer in the room except the sender.

        A failed send (recipient's socket died but its handler hasn't
        cleaned up yet) is logged and skipped rather than propagated —
        it's the recipient's problem, not the sender's.
        """
        # Copy: a send can yield, and the dict may change underneath us
        for pid, ws in list(self._connections.get(room_id, {}).items()):
            if pid == sender_id:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                logger.warning("relay to peer %s in room %s failed", pid, room_id, exc_info=True)

    def is_connected(self, room_id: str, peer_id: str) -> bool:
        """Check if a peer currently has an active WebSocket."""
        return peer_id in self._connections.get(room_id, {})

    async def shutdown(self) -> None:
        """Cancel pending slot releases (server shutdown)."""
        tasks = list(self._release_tasks.values())
        self._release_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
