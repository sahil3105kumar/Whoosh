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


def test_config_returns_ice_servers():
    response = client.get("/config")

    assert response.status_code == 200
    ice_servers = response.json()["ice_servers"]
    assert ice_servers and all(url.startswith("stun:") for url in ice_servers[0]["urls"])


def test_config_includes_turn_credentials(monkeypatch):
    import base64
    import hashlib
    import hmac

    from apps.server import main
    from apps.server.config import Settings

    monkeypatch.setattr(
        main,
        "settings",
        Settings(turn_urls=("turn:turn.example:3478",), turn_secret="shh", turn_ttl_seconds=600),
    )
    turn = client.get("/config").json()["ice_servers"][-1]

    assert turn["urls"] == ["turn:turn.example:3478"]
    expiry = int(turn["username"].split(":")[0])
    assert 0 < expiry - __import__("time").time() <= 600
    expected = base64.b64encode(
        hmac.new(b"shh", turn["username"].encode(), hashlib.sha1).digest()
    ).decode()
    assert turn["credential"] == expected
    assert "shh" not in client.get("/config").text  # the secret itself never leaves


def test_config_without_turn_has_only_stun():
    servers = client.get("/config").json()["ice_servers"]
    assert all("username" not in s or s["username"] is None for s in servers)
