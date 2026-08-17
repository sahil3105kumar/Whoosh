from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from packages.signaling.exceptions import RoomExpiredError, RoomNotFoundError
from packages.signaling.room_manager import RoomManager

app = FastAPI(title="Whoosh Signaling Server")

# Single process-wide instance. Fine for v1.0 (single-instance
# deployment); revisit if
# this ever runs behind a multi-instance autoscaler — see the note
# in room_manager.py.
room_manager = RoomManager()


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


@app.get("/healthz")
async def healthz() -> dict[str, str | float]:
    """Liveness check for deployment (Docker healthcheck / reverse
    proxy). Bundled in now since it's near-zero cost and every
    deployment guide you'll follow later expects one to exist.
    """
    return {"status": "ok", "time": time.time()}
