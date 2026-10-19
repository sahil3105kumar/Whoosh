from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from apps.server.config import Settings
from apps.server.rate_limit import RateLimiter
from packages.signaling.connection_manager import ConnectionManager
from packages.signaling.exceptions import (
    RoomExpiredError,
    RoomFullError,
    RoomNotFoundError,
)
from packages.signaling.room_manager import RoomManager

logger = logging.getLogger(__name__)

settings = Settings.from_env()
logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")

#: How often the background sweep evicts expired rooms.
_SWEEP_INTERVAL_SECONDS = 60.0

#: Message types peers may relay through the server. Everything else
#: (peer_joined, peer_left, ...) is server-originated only.
RELAY_TYPES = frozenset({"offer", "answer", "ice"})

# Single process-wide instance. Fine for v1.0 (single-instance
# deployment); revisit if
# this ever runs behind a multi-instance autoscaler — see the note
# in room_manager.py.
room_manager = RoomManager(ttl_seconds=settings.room_ttl_seconds)
connection_manager = ConnectionManager(
    room_manager, reconnect_grace_seconds=settings.reconnect_grace_seconds
)
room_create_limiter = RateLimiter(settings.room_create_per_minute)


async def _sweep_expired_rooms() -> None:
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
        # Occupied rooms never expire (D51); only abandoned ones do
        for room_id in connection_manager.active_rooms():
            room_manager.touch(room_id)
        evicted = room_manager.evict_expired()
        room_create_limiter.prune()
        if evicted:
            logger.info("evicted %d expired room(s)", evicted)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    sweeper = asyncio.create_task(_sweep_expired_rooms())
    try:
        yield
    finally:
        sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeper
        await connection_manager.shutdown()


app = FastAPI(title="Whoosh Signaling Server", lifespan=lifespan)

if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST"],
    )


class CreateRoomResponse(BaseModel):
    room_id: str
    state: str
    created_at: float
    expires_at: float


@app.post("/rooms", response_model=CreateRoomResponse, status_code=201)
async def create_room(request: Request) -> CreateRoomResponse:
    """Create a new signaling room.

    No request body: room creation takes no parameters in v1.0 (no
    auth, no room naming — see docs/vision.md: "no accounts or
    authentication"). Returns the room_id the creator shares with
    whoever they want to connect with.
    """
    client_ip = request.client.host if request.client else "unknown"
    if not room_create_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="too many rooms created, slow down")

    room = room_manager.create_room()
    return CreateRoomResponse(
        room_id=room.id,
        state=str(room.state),
        created_at=room.created_at,
        expires_at=room.expires_at,
    )


@app.get("/rooms/{room_id}")
async def get_room(room_id: str) -> CreateRoomResponse:
    """Look up a room's current state.

    Not strictly required by issue #2, but nearly free to add
    alongside create_room() and useful for the web/CLI clients to
    check "does this room_id still exist?" before attempting to
    join. Raises 404 for both unknown and expired rooms — see the
    docstring on RoomExpiredError for why those are distinct
    exceptions internally even though they map to the same HTTP
    status here.
    """
    try:
        room = room_manager.get_room(room_id)
    except RoomNotFoundError as exc:
        raise HTTPException(status_code=404, detail="room not found") from exc
    except RoomExpiredError as exc:
        raise HTTPException(status_code=404, detail="room expired") from exc

    return CreateRoomResponse(
        room_id=room.id,
        state=str(room.state),
        created_at=room.created_at,
        expires_at=room.expires_at,
    )


class _CloseConnection(Exception):
    """Raised by _receive_signal when the peer broke the framing rules."""

    def __init__(self, code: int, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason)


async def _receive_signal(websocket: WebSocket) -> dict[str, Any]:
    """Receive one signaling message: a text frame holding a JSON object
    no larger than ``settings.max_message_bytes``."""
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))

    text = message.get("text")
    if text is None:
        raise _CloseConnection(1003, "binary frames not supported")
    if len(text.encode()) > settings.max_message_bytes:
        raise _CloseConnection(1009, "message too big")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _CloseConnection(1007, "invalid JSON") from exc
    if not isinstance(data, dict):
        raise _CloseConnection(1007, "expected a JSON object")
    return data


@app.websocket("/rooms/{room_id}/ws")
async def ws_signaling(
    websocket: WebSocket,
    room_id: str,
    peer_id: str | None = None,
) -> None:
    """WebSocket endpoint for signaling relay.

    Peers connect here to exchange SDP offers/answers and ICE
    candidates. The server checks only framing, size, and that the
    ``type`` is one of RELAY_TYPES — it never looks inside SDP or
    candidates.

    Query params:
        peer_id: Optional. If provided, treated as a reconnection
                 for an existing peer slot.
    """
    await websocket.accept()

    if not settings.origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008, reason="origin not allowed")
        return

    try:
        assigned_peer_id = await connection_manager.connect(room_id, websocket, peer_id)
    except (RoomNotFoundError, RoomExpiredError, RoomFullError) as exc:
        await websocket.close(code=1008, reason=str(exc))
        return

    # Tell the client their assigned peer_id so they can reconnect later
    await websocket.send_json(
        {
            "type": "hello",
            "peer_id": assigned_peer_id,
        }
    )

    # Always unregister, whatever ends the loop — otherwise a bad
    # frame leaves a dead socket registered and the slot held.
    try:
        while True:
            data = await _receive_signal(websocket)
            msg_type = data.get("type")
            if msg_type not in RELAY_TYPES:
                await websocket.send_json(
                    {"type": "error", "reason": f"cannot relay message type {msg_type!r}"}
                )
                continue
            await connection_manager.relay(room_id, assigned_peer_id, data)
    except WebSocketDisconnect:
        pass
    except _CloseConnection as exc:
        await websocket.close(code=exc.code, reason=exc.reason)
    finally:
        await connection_manager.disconnect(room_id, assigned_peer_id, websocket)


class IceServer(BaseModel):
    """Same shape as the browser's RTCIceServer dictionary."""

    urls: list[str]


class ClientConfigResponse(BaseModel):
    ice_servers: list[IceServer]


@app.get("/config")
async def client_config() -> ClientConfigResponse:
    """Runtime config clients fetch before connecting.

    Serving ICE servers from here (rather than hardcoding them in each
    client) means adding TURN later is a server config change only.
    """
    ice_servers = [IceServer(urls=list(settings.stun_urls))] if settings.stun_urls else []
    return ClientConfigResponse(ice_servers=ice_servers)


@app.get("/healthz")
async def healthz() -> dict[str, str | float]:
    """Liveness check for deployment (Docker healthcheck / reverse
    proxy). Bundled in now since it's near-zero cost and every
    deployment guide you'll follow later expects one to exist.
    """
    return {"status": "ok", "time": time.time()}


# Last, so every API route above takes precedence over static files.
if settings.web_dir:
    app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
