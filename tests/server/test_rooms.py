"""
API-level tests for apps/server/main.py — exercises the actual HTTP
endpoints via FastAPI's TestClient, so this catches request/response
shape bugs the unit tests in tests/signaling/ wouldn't.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.server.main import app, room_manager

client = TestClient(app)


def test_create_room_returns_201_with_room_id():
    response = client.post("/rooms")

    assert response.status_code == 201
    body = response.json()
    assert "room_id" in body and body["room_id"]
    assert body["state"] == "created"
    assert body["expires_at"] > body["created_at"]


def test_get_room_after_creation_returns_same_room():
    created = client.post("/rooms").json()

    response = client.get(f"/rooms/{created['room_id']}")

    assert response.status_code == 200
    assert response.json()["room_id"] == created["room_id"]


def test_get_unknown_room_returns_404():
    response = client.get("/rooms/does-not-exist")
    assert response.status_code == 404


def test_healthz_returns_ok():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_room_registers_with_room_manager():
    before = room_manager.room_count()
    client.post("/rooms")
    after = room_manager.room_count()
    assert after == before + 1
