"""
Signaling + WebRTC setup for the CLI: get from a room id to an open
DataChannel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import websockets
from aiortc import RTCConfiguration, RTCIceServer
from websockets.exceptions import ConnectionClosed

from packages.rtc.peer import WebRTCPeer

log = logging.getLogger(__name__)

#: Seconds allowed from the start of negotiation to an open DataChannel.
#: Covers STUN candidate gathering plus ICE connectivity checks.
CONNECT_TIMEOUT = 30.0


class ConnectError(Exception):
    """Couldn't establish the P2P connection. The message is user-facing."""


@dataclass
class Connection:
    peer: WebRTCPeer
    #: Background task reading the signaling socket. It finishing normally
    #: (server went away) doesn't affect the P2P link; an exception does.
    signaling: asyncio.Task[None]


# ── HTTP helpers (stdlib only, no requests/httpx needed) ─────────────


def explain_close(reason: str, room_id: str) -> str:
    """Turn the server's WebSocket close reason into advice for a person."""
    if reason.startswith("No such room"):
        return (
            f"Room {room_id} doesn't exist. Check the code; rooms also expire "
            "10 minutes after they're created."
        )
    if reason.startswith("Room expired"):
        return f"Room {room_id} has expired. Ask for a new code."
    if reason.startswith("Room is full"):
        return f"Room {room_id} already has two people in it."
    return reason


def create_room(server: str) -> str:
    req = urllib.request.Request(f"{server}/rooms", method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            room_id: str = json.loads(resp.read())["room_id"]
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise ConnectError(
                "Too many rooms created from your address. Wait a minute and try again."
            ) from exc
        raise
    return room_id


def _fetch_rtc_config(server: str) -> RTCConfiguration:
    with urllib.request.urlopen(f"{server}/config") as resp:
        ice_servers = json.loads(resp.read())["ice_servers"]
    return RTCConfiguration(iceServers=[RTCIceServer(urls=s["urls"]) for s in ice_servers])


# ── Connect ─────────────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def connect(server: str, room_id: str, *, is_creator: bool) -> AsyncIterator[Connection]:
    """Join ``room_id`` and yield once the DataChannel is open.

    Waits indefinitely for the other peer to arrive, then allows
    CONNECT_TIMEOUT for the connection itself. Raises ConnectError.
    """
    ws_url = f"{server.replace('http', 'ws', 1)}/rooms/{room_id}/ws"
    rtc_config = _fetch_rtc_config(server)

    async with websockets.connect(ws_url) as ws:
        peer = WebRTCPeer(
            on_signaling_message=lambda msg: ws.send(json.dumps(msg)),
            config=rtc_config,
        )
        # Resolved once the other peer shows up (creator: peer_joined,
        # joiner: offer). Until then we wait indefinitely.
        negotiation_started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        close_reason = ""

        async def _listen_signaling() -> None:
            nonlocal close_reason
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type")

                    if t in ("peer_joined", "offer") and not negotiation_started.done():
                        negotiation_started.set_result(None)

                    if t == "hello":
                        log.debug("signaling connected (peer_id=%s)", msg["peer_id"])

                    elif t == "peer_joined":
                        if is_creator:
                            log.info("Peer joined — connecting...")
                            await peer.create_offer()

                    elif t in ("offer", "answer"):
                        await peer.handle_signaling(msg)

                    elif t == "peer_disconnected":
                        log.debug("peer dropped from signaling")

                    elif t == "peer_left":
                        log.debug("peer left the room")

                    elif t == "error":
                        log.error("Signaling server: %s", msg.get("reason"))
            except ConnectionClosed as exc:
                close_reason = exc.rcvd.reason if exc.rcvd else ""

        listen_task = asyncio.create_task(_listen_signaling())

        def _closed_message(what: str) -> str:
            if close_reason:
                return explain_close(close_reason, room_id)
            return f"The signaling server closed the connection {what}."

        try:
            if not await _wait_or_fail(negotiation_started, listen_task):
                raise ConnectError(_closed_message("before a peer arrived"))

            if not await _wait_or_fail(peer.channel_ready, listen_task, timeout=CONNECT_TIMEOUT):
                if peer.pc.iceConnectionState == "failed":
                    raise ConnectError(
                        "Direct connection failed: no network path between the peers. "
                        "One side is likely behind a strict NAT or firewall "
                        "(relay servers aren't supported yet)."
                    )
                if listen_task.done():
                    raise ConnectError(_closed_message("before the connection opened"))
                raise ConnectError("Timed out establishing the connection.")

            yield Connection(peer=peer, signaling=listen_task)
        finally:
            listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listen_task
            await peer.close()


async def _wait_or_fail(
    fut: asyncio.Future[Any],
    listen_task: asyncio.Task[None],
    timeout: float | None = None,
) -> bool:
    """Wait for ``fut``, racing it against the signaling listener so a
    listener failure is re-raised immediately rather than surfacing as a
    timeout. Returns whether ``fut`` completed."""
    waiters: set[asyncio.Future[Any]] = {fut, listen_task}
    await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    if listen_task.done():
        listen_task.result()  # re-raise the listener's exception, if any
    return fut.done()
