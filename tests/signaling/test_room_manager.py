"""
Unit tests for packages/signaling/room_manager.py.

No FastAPI, no network — these test RoomManager as a plain Python
object, matching the "no server, no RTC, just pytest" style used for
the protocol codec tests.
"""

from __future__ import annotations

import pytest

from packages.signaling.enums import RoomState
from packages.signaling.exceptions import RoomExpiredError, RoomNotFoundError
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
