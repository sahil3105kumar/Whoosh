from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from packages.signaling.connection_manager import ConnectionManager
from packages.signaling.exceptions import (
    RoomExpiredError,
    RoomFullError,
    RoomNotFoundError,
)
from packages.signaling.room_manager import RoomManager

app = FastAPI(title="Whoosh Signaling Server")

# Single process-wide instance. Fine for v1.0 (single-instance
# deployment); revisit if
# this ever runs behind a multi-instance autoscaler — see the note
# in room_manager.py.
room_manager = RoomManager()
connection_manager = ConnectionManager(room_manager)


class CreateRoomResponse(BaseModel):
    room_id: str
    state: str
    created_at: float
    expires_at: float


@app.post("/rooms", response_model=CreateRoomResponse, status_code=201)
async def create_room() -> CreateRoomResponse:
    """Create a new signaling room.

    No request body: room creation takes no parameters in v1.0 (no
    auth, no room naming — see docs/vision.md: "no accounts or
    authentication"). Returns the room_id the creator shares with
    whoever they want to connect with.
    """
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


@app.websocket("/rooms/{room_id}/ws")
async def ws_signaling(
    websocket: WebSocket,
    room_id: str,
    peer_id: str | None = None,
) -> None:
    """WebSocket endpoint for signaling relay.

    Peers connect here to exchange SDP offers/answers and ICE
    candidates. The server never inspects payloads — it blindly
    forwards JSON from one peer to the other.

    Query params:
        peer_id: Optional. If provided, treated as a reconnection
                 for an existing peer slot.
    """
    await websocket.accept()

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

    try:
        while True:
            data = await websocket.receive_json()
            await connection_manager.relay(room_id, assigned_peer_id, data)
    except WebSocketDisconnect:
        await connection_manager.disconnect(room_id, assigned_peer_id)


@app.get("/healthz")
async def healthz() -> dict[str, str | float]:
    """Liveness check for deployment (Docker healthcheck / reverse
    proxy). Bundled in now since it's near-zero cost and every
    deployment guide you'll follow later expects one to exist.
    """
    return {"status": "ok", "time": time.time()}
