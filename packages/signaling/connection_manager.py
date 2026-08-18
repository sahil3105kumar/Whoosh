"""
WebSocket connection manager for signaling rooms.

Tracks which peers have active WebSocket connections and handles
message relay. Peers remain in the room even after their WebSocket
disconnects — only the live socket reference is removed.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket

    from .room_manager import RoomManager


class ConnectionManager:
    """Maps room_id -> {peer_id -> WebSocket} for active connections."""

    def __init__(self, room_manager: RoomManager) -> None:
        self._room_manager = room_manager
        # room_id -> {peer_id -> WebSocket}
        self._connections: dict[str, dict[str, WebSocket]] = {}

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
        calling join_room again.

        Returns the peer_id (generated or reused).

        Raises RoomNotFoundError, RoomExpiredError, or RoomFullError
        on failure — callers should catch and close the WebSocket.
        """
        room = self._room_manager.get_room(room_id)

        if peer_id is not None and peer_id in room.peer_ids:
            # Reconnection: peer already has a slot, just update the socket
            pass
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
        await self._broadcast(
            room_id,
            sender_id=peer_id,
            message={"type": "peer_joined", "peer_id": peer_id},
        )

        return peer_id

    async def disconnect(self, room_id: str, peer_id: str) -> None:
        """Remove the WebSocket reference without leaving the room.

        The peer's slot in the room is preserved for reconnection.
        Notifies the remaining peer that signaling dropped.
        """
        room_peers = self._connections.get(room_id)
        if room_peers is None:
            return

        room_peers.pop(peer_id, None)

        # Notify remaining connected peers
        await self._broadcast(
            room_id,
            sender_id=peer_id,
            message={"type": "peer_disconnected", "peer_id": peer_id},
        )

        # Clean up the dict entry if no active connections remain
        if not room_peers:
            del self._connections[room_id]

    async def relay(
        self,
        room_id: str,
        sender_id: str,
        message: dict,
    ) -> None:
        """Forward a message to all other connected peers in the room."""
        await self._broadcast(room_id, sender_id=sender_id, message=message)

    def is_connected(self, room_id: str, peer_id: str) -> bool:
        """Check if a peer currently has an active WebSocket."""
        return peer_id in self._connections.get(room_id, {})

    async def _broadcast(
        self,
        room_id: str,
        *,
        sender_id: str,
        message: dict,
    ) -> None:
        """Send a JSON message to every connected peer except the sender."""
        room_peers = self._connections.get(room_id, {})
        for pid, ws in room_peers.items():
            if pid != sender_id:
                await ws.send_json(message)
