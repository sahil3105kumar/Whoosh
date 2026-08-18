"""
Unit tests for packages/signaling/connection_manager.py.

Uses a mock WebSocket to test ConnectionManager in isolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from packages.signaling.connection_manager import ConnectionManager
from packages.signaling.enums import RoomState
from packages.signaling.exceptions import RoomFullError
from packages.signaling.room_manager import RoomManager


def _make_ws() -> AsyncMock:
    """Create a mock WebSocket."""
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


@pytest.fixture
def manager() -> tuple[RoomManager, ConnectionManager]:
    rm = RoomManager()
    cm = ConnectionManager(rm)
    return rm, cm


async def test_connect_assigns_peer_id(manager):
    rm, cm = manager
    room = rm.create_room()
    ws = _make_ws()

    peer_id = await cm.connect(room.id, ws)

    assert peer_id  # non-empty string
    assert peer_id in room.peer_ids
    assert room.state == RoomState.WAITING


async def test_connect_two_peers_transitions_to_connected(manager):
    rm, cm = manager
    room = rm.create_room()

    pid1 = await cm.connect(room.id, _make_ws())
    pid2 = await cm.connect(room.id, _make_ws())

    assert room.state == RoomState.CONNECTED
    assert room.peer_ids == {pid1, pid2}


async def test_third_peer_raises_room_full(manager):
    rm, cm = manager
    room = rm.create_room()

    await cm.connect(room.id, _make_ws())
    await cm.connect(room.id, _make_ws())

    with pytest.raises(RoomFullError):
        await cm.connect(room.id, _make_ws())


async def test_reconnect_with_existing_peer_id(manager):
    rm, cm = manager
    room = rm.create_room()
    ws1 = _make_ws()

    pid = await cm.connect(room.id, ws1)

    # Simulate disconnect
    await cm.disconnect(room.id, pid)
    assert not cm.is_connected(room.id, pid)

    # Reconnect with same peer_id
    ws2 = _make_ws()
    returned_pid = await cm.connect(room.id, ws2, peer_id=pid)

    assert returned_pid == pid
    assert cm.is_connected(room.id, pid)
    # Still only 1 peer in the room (not duplicated)
    assert len(room.peer_ids) == 1


async def test_relay_forwards_to_other_peer(manager):
    rm, cm = manager
    room = rm.create_room()
    ws_a = _make_ws()
    ws_b = _make_ws()

    pid_a = await cm.connect(room.id, ws_a)
    await cm.connect(room.id, ws_b)

    msg = {"type": "offer", "sdp": "..."}
    await cm.relay(room.id, pid_a, msg)

    # B should have received it, A should not
    ws_b.send_json.assert_called_with(msg)
    # A's send_json was called for peer_joined, not for relay
    for call in ws_a.send_json.call_args_list:
        assert call.args[0].get("type") != "offer"


async def test_disconnect_notifies_remaining_peer(manager):
    rm, cm = manager
    room = rm.create_room()
    ws_a = _make_ws()
    ws_b = _make_ws()

    pid_a = await cm.connect(room.id, ws_a)
    await cm.connect(room.id, ws_b)

    # Reset mocks to only track disconnect notification
    ws_a.send_json.reset_mock()
    ws_b.send_json.reset_mock()

    await cm.disconnect(room.id, pid_a)

    # B should receive peer_disconnected
    ws_b.send_json.assert_called_once_with({"type": "peer_disconnected", "peer_id": pid_a})

    # A's slot is still in the room (preserved for reconnect)
    assert pid_a in room.peer_ids


async def test_disconnect_does_not_remove_from_room(manager):
    rm, cm = manager
    room = rm.create_room()
    ws = _make_ws()

    pid = await cm.connect(room.id, ws)
    await cm.disconnect(room.id, pid)

    # Peer still in room, just no active WebSocket
    assert pid in room.peer_ids
    assert not cm.is_connected(room.id, pid)


async def test_disconnect_unknown_room_is_noop(manager):
    _, cm = manager
    await cm.disconnect("nonexistent", "peer1")  # should not raise
