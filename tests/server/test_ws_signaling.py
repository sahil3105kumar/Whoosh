"""
Integration tests for the WebSocket signaling endpoint.

Uses httpx-ws for proper async WebSocket testing, which avoids
the deadlock issues with Starlette's sync TestClient when multiple
WebSocket connections need to exchange messages concurrently.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from httpx_ws import aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport

from apps.server.main import app

# Async HTTP client for room creation
transport = ASGIWebSocketTransport(app=app)


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _create_room() -> str:
    """Helper: create a room via HTTP and return its room_id."""
    async with _http_client() as client:
        resp = await client.post("/rooms")
        assert resp.status_code == 201
        return resp.json()["room_id"]


async def test_peer_receives_hello_with_peer_id():
    room_id = await _create_room()

    async with _http_client() as client:
        async with aconnect_ws(f"/rooms/{room_id}/ws", client) as ws:
            msg = await ws.receive_json()
            assert msg["type"] == "hello"
            assert "peer_id" in msg
            assert msg["peer_id"]


async def test_two_peers_can_relay_messages():
    room_id = await _create_room()
    results: dict[str, object] = {}

    async def peer_a():
        async with _http_client() as client:
            async with aconnect_ws(f"/rooms/{room_id}/ws", client) as ws:
                hello = await ws.receive_json()
                results["a_pid"] = hello["peer_id"]

                # Wait for peer_joined
                joined = await ws.receive_json()
                results["a_got_joined"] = joined["type"]

                # Send offer
                await ws.send_json({"type": "offer", "sdp": "test-sdp"})

                # Receive answer
                answer = await ws.receive_json()
                results["a_got_answer"] = answer

    async def peer_b():
        await asyncio.sleep(0.05)  # Let A connect first
        async with _http_client() as client:
            async with aconnect_ws(f"/rooms/{room_id}/ws", client) as ws:
                hello = await ws.receive_json()
                results["b_pid"] = hello["peer_id"]

                # Receive offer from A
                offer = await ws.receive_json()
                results["b_got_offer"] = offer

                # Send answer
                await ws.send_json({"type": "answer", "sdp": "answer-sdp"})

    await asyncio.gather(peer_a(), peer_b())

    assert results["a_got_joined"] == "peer_joined"
    assert results["b_got_offer"]["type"] == "offer"
    assert results["b_got_offer"]["sdp"] == "test-sdp"
    assert results["a_got_answer"]["type"] == "answer"
    assert results["a_got_answer"]["sdp"] == "answer-sdp"


async def test_nonexistent_room_closes_with_reason():
    from httpx_ws import WebSocketDisconnect

    with pytest.raises(ExceptionGroup) as exc_info:
        async with _http_client() as client:
            async with aconnect_ws("/rooms/does-not-exist/ws", client) as ws:
                await ws.receive_json()

    # Dig into the nested ExceptionGroup to find the WebSocketDisconnect
    exceptions = exc_info.value.exceptions
    # Flatten nested groups
    while exceptions and isinstance(exceptions[0], ExceptionGroup):
        exceptions = exceptions[0].exceptions
    assert len(exceptions) == 1
    ws_exc = exceptions[0]
    assert isinstance(ws_exc, WebSocketDisconnect)
    assert ws_exc.code.value == 1008
    assert "No such room" in ws_exc.reason


async def test_peer_disconnect_notifies_other():
    room_id = await _create_room()
    results: dict[str, object] = {}

    async def peer_a():
        async with _http_client() as client:
            async with aconnect_ws(f"/rooms/{room_id}/ws", client) as ws:
                await ws.receive_json()  # hello
                await ws.receive_json()  # peer_joined
                notif = await ws.receive_json()  # peer_disconnected
                results["notif"] = notif

    async def peer_b():
        await asyncio.sleep(0.05)
        async with _http_client() as client:
            async with aconnect_ws(f"/rooms/{room_id}/ws", client) as ws:
                await ws.receive_json()  # hello
                await asyncio.sleep(0.05)
            # B disconnects

    await asyncio.gather(peer_a(), peer_b())
    assert results["notif"]["type"] == "peer_disconnected"


async def test_reconnect_with_peer_id():
    room_id = await _create_room()

    async with _http_client() as client:
        async with aconnect_ws(f"/rooms/{room_id}/ws", client) as ws:
            hello = await ws.receive_json()
            peer_id = hello["peer_id"]

    # Reconnect with same peer_id
    async with _http_client() as client:
        async with aconnect_ws(f"/rooms/{room_id}/ws?peer_id={peer_id}", client) as ws:
            hello2 = await ws.receive_json()
            assert hello2["peer_id"] == peer_id


async def test_invalid_json_frees_socket_registration():
    from httpx_ws import WebSocketDisconnect

    from apps.server.main import connection_manager

    room_id = await _create_room()

    async with _http_client() as client:
        async with aconnect_ws(f"/rooms/{room_id}/ws", client) as ws:
            hello = await ws.receive_json()
            await ws.send_text("not json")
            with pytest.raises(WebSocketDisconnect) as exc_info:
                await ws.receive_json()
            assert exc_info.value.code == 1007

    assert not connection_manager.is_connected(room_id, hello["peer_id"])


async def _expect_close(ws, code: int) -> None:
    from httpx_ws import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        await ws.receive_json()
    assert exc_info.value.code == code


async def test_unrelayable_type_gets_error_and_connection_stays_open():
    room_id = await _create_room()

    async with _http_client() as client:
        async with aconnect_ws(f"/rooms/{room_id}/ws", client) as ws:
            await ws.receive_json()  # hello
            await ws.send_json({"type": "peer_joined", "peer_id": "spoofed"})
            err = await ws.receive_json()
            assert err["type"] == "error"
            assert "peer_joined" in err["reason"]

            # Still usable afterwards
            await ws.send_json({"type": "nope"})
            assert (await ws.receive_json())["type"] == "error"


async def test_oversized_message_closes_with_1009(monkeypatch: pytest.MonkeyPatch):
    from apps.server import main
    from apps.server.config import Settings

    monkeypatch.setattr(main, "settings", Settings(max_message_bytes=100))
    room_id = await _create_room()

    async with _http_client() as client:
        async with aconnect_ws(f"/rooms/{room_id}/ws", client) as ws:
            await ws.receive_json()  # hello
            await ws.send_json({"type": "offer", "sdp": "x" * 200})
            await _expect_close(ws, 1009)


async def test_binary_frame_closes_with_1003():
    room_id = await _create_room()

    async with _http_client() as client:
        async with aconnect_ws(f"/rooms/{room_id}/ws", client) as ws:
            await ws.receive_json()  # hello
            await ws.send_bytes(b"\x00\x01")
            await _expect_close(ws, 1003)


async def test_disallowed_origin_closes_with_1008(monkeypatch: pytest.MonkeyPatch):
    from apps.server import main
    from apps.server.config import Settings

    monkeypatch.setattr(main, "settings", Settings(allowed_origins=("https://ok.example",)))
    room_id = await _create_room()

    async with _http_client() as client:
        async with aconnect_ws(
            f"/rooms/{room_id}/ws", client, headers={"Origin": "https://evil.example"}
        ) as ws:
            await _expect_close(ws, 1008)


async def test_hello_lists_peers_already_connected():
    room_id = await _create_room()

    async with _http_client() as client:
        async with aconnect_ws(f"/rooms/{room_id}/ws", client) as first:
            first_hello = await first.receive_json()
            assert first_hello["peers"] == []
            async with _http_client() as client2:
                async with aconnect_ws(f"/rooms/{room_id}/ws", client2) as second:
                    second_hello = await second.receive_json()
                    assert second_hello["peers"] == [first_hello["peer_id"]]


async def test_addressed_signaling_reaches_only_its_target():
    room_id = await _create_room()
    async with _http_client() as c1, _http_client() as c2, _http_client() as c3:
        async with (
            aconnect_ws(f"/rooms/{room_id}/ws", c1) as a,
            aconnect_ws(f"/rooms/{room_id}/ws", c2) as b,
            aconnect_ws(f"/rooms/{room_id}/ws", c3) as c,
        ):
            a_id = (await a.receive_json())["peer_id"]
            b_id = (await b.receive_json())["peer_id"]
            await c.receive_json()
            # drain peer_joined notices
            for _ in range(2):
                await a.receive_json()
            await b.receive_json()

            await a.send_json({"type": "offer", "sdp": "for-b", "to": b_id})
            msg = await b.receive_json()
            assert msg == {"type": "offer", "sdp": "for-b", "to": b_id, "from": a_id}

            await a.send_json({"type": "offer", "sdp": "x", "to": "nobody"})
            err = await a.receive_json()
            assert err["type"] == "error" and "nobody" in err["reason"]

            # c got nothing addressed to b
            await a.send_json({"type": "offer", "sdp": "for-c-probe", "to": b_id})
            assert (await b.receive_json())["sdp"] == "for-c-probe"
