"""
Unit tests for packages/signaling/room_manager.py.

No FastAPI, no network — these test RoomManager as a plain Python
object, matching the "no server, no RTC, just pytest" style used for
the protocol codec tests.
"""

from __future__ import annotations

import pytest

from packages.signaling.enums import RoomState
from packages.signaling.exceptions import (
    RoomExpiredError,
    RoomFullError,
    RoomNotFoundError,
)
from packages.signaling.room_manager import RoomManager


def test_create_room_returns_room_in_created_state():
    manager = RoomManager()
    room = manager.create_room()

    assert room.state == RoomState.CREATED
    assert room.peer_ids == set()
    assert room.expires_at > room.created_at


def test_created_rooms_have_unique_ids():
    manager = RoomManager()
    ids = {manager.create_room().id for _ in range(50)}
    assert len(ids) == 50


def test_get_room_returns_the_room_that_was_created():
    manager = RoomManager()
    created = manager.create_room()

    fetched = manager.get_room(created.id)

    assert fetched.id == created.id
    assert fetched is created  # same object, not a copy


def test_get_room_raises_for_unknown_id():
    manager = RoomManager()
    with pytest.raises(RoomNotFoundError):
        manager.get_room("does-not-exist")


def test_get_room_raises_for_expired_room_and_evicts_it():
    manager = RoomManager(ttl_seconds=1)
    room = manager.create_room()

    # Simulate time passing without sleeping in the test: query using
    # an "is_expired" check directly first to confirm the setup, then
    # force expiry by shrinking the room's own expires_at.
    room.expires_at = room.created_at - 1  # already in the past

    with pytest.raises(RoomExpiredError):
        manager.get_room(room.id)

    # Second lookup is now RoomNotFoundError, not RoomExpiredError,
    # because the expired room was evicted on the first lookup.
    with pytest.raises(RoomNotFoundError):
        manager.get_room(room.id)


def test_room_count_reflects_created_rooms():
    manager = RoomManager()
    assert manager.room_count() == 0

    manager.create_room()
    manager.create_room()

    assert manager.room_count() == 2


# ---------------------------------------------------------------------------
# join_room
# ---------------------------------------------------------------------------


def test_first_peer_joins_transitions_to_waiting():
    manager = RoomManager()
    room = manager.create_room()

    manager.join_room(room.id, "peer1")

    assert room.state == RoomState.WAITING
    assert "peer1" in room.peer_ids


def test_second_peer_joins_transitions_to_connected():
    manager = RoomManager()
    room = manager.create_room()

    manager.join_room(room.id, "peer1")
    manager.join_room(room.id, "peer2")

    assert room.state == RoomState.CONNECTED
    assert room.peer_ids == {"peer1", "peer2"}


def test_third_peer_raises_room_full():
    manager = RoomManager()
    room = manager.create_room()

    manager.join_room(room.id, "peer1")
    manager.join_room(room.id, "peer2")

    with pytest.raises(RoomFullError):
        manager.join_room(room.id, "peer3")


# ---------------------------------------------------------------------------
# leave_room
# ---------------------------------------------------------------------------


def test_one_peer_leaves_room_stays_open():
    manager = RoomManager()
    room = manager.create_room()
    manager.join_room(room.id, "peer1")
    manager.join_room(room.id, "peer2")

    manager.leave_room(room.id, "peer1")

    # Room stays alive, transitions back to WAITING
    assert room.state == RoomState.WAITING
    assert room.peer_ids == {"peer2"}
    assert manager.room_count() == 1


def test_all_peers_leave_room_is_expired_and_evicted():
    manager = RoomManager()
    room = manager.create_room()
    manager.join_room(room.id, "peer1")
    manager.join_room(room.id, "peer2")

    manager.leave_room(room.id, "peer1")
    manager.leave_room(room.id, "peer2")

    with pytest.raises(RoomNotFoundError):
        manager.get_room(room.id)
    assert manager.room_count() == 0


def test_leave_unknown_room_is_noop():
    manager = RoomManager()
    manager.leave_room("does-not-exist", "peer1")  # should not raise


def test_leave_unknown_peer_is_noop():
    manager = RoomManager()
    room = manager.create_room()
    manager.join_room(room.id, "peer1")

    manager.leave_room(room.id, "peer99")  # peer99 never joined

    # Room unchanged
    assert room.state == RoomState.WAITING
    assert room.peer_ids == {"peer1"}
