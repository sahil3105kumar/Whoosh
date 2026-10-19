"""
Signaling + WebRTC for the CLI: get from a room id to an open DataChannel,
and keep getting back there when the connection drops (#19, D50).

``Link`` owns the signaling WebSocket and the current WebRTC connection.
When the peer-to-peer connection dies, the room creator re-offers as soon
as both sides are reachable through signaling, and the joiner accepts any
new offer; a dropped WebSocket reconnects with its peer_id so it keeps its
slot in the room.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

import websockets
from aiortc import RTCConfiguration, RTCIceServer
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from packages.rtc.peer import WebRTCPeer

log = logging.getLogger(__name__)

#: Seconds allowed from the start of negotiation to an open DataChannel.
#: Covers STUN candidate gathering plus ICE connectivity checks.
CONNECT_TIMEOUT = 30.0

#: How long ``reconnect`` keeps trying after the connection drops.
RECONNECT_TIMEOUT = 60.0

#: WebSocket close code the server uses for "room not found / expired / full".
_POLICY_VIOLATION = 1008


class ConnectError(Exception):
    """Couldn't establish the P2P connection. The message is user-facing."""


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


_DIRECT_FAILED = (
    "Direct connection failed: no network path between the peers. "
    "One side is likely behind a strict NAT or firewall "
    "(relay servers aren't supported yet)."
)


# ── Link ────────────────────────────────────────────────────────────


class Link:
    """A room's connection to the other peer, re-established when it drops."""

    def __init__(self, server: str, room_id: str, *, is_creator: bool) -> None:
        self.server = server
        self.room_id = room_id
        self.is_creator = is_creator
        self._ws_url = f"{server.replace('http', 'ws', 1)}/rooms/{room_id}/ws"
        self._rtc_config: RTCConfiguration | None = None
        self._ws: ClientConnection | None = None
        #: Our slot in the room, reused when the WebSocket reconnects.
        self._peer_id: str | None = None
        #: The current WebRTC connection (maybe still negotiating)...
        self._peer: WebRTCPeer | None = None
        #: ...and the same object once its DataChannel is open.
        self._ready: WebRTCPeer | None = None
        #: Creator: the other peer has been in the room, so it's worth offering.
        self._other_seen = False
        self._negotiation_started: float | None = None
        self._last_ice_failed = False
        #: Signaling can't work any more (room gone, server unreachable).
        self._fatal: ConnectError | None = None
        self._closing = False
        self._changed = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._signaling: asyncio.Task[None] | None = None

    # ── Public API ──────────────────────────────────────────────────

    async def open(self) -> WebRTCPeer:
        """First connection. Waits indefinitely for the other person to
        arrive, then CONNECT_TIMEOUT for the connection itself."""
        self._rtc_config = await asyncio.to_thread(_fetch_rtc_config, self.server)
        self._signaling = asyncio.create_task(self._signaling_loop())
        return await self._wait_ready(first=True)

    async def reconnect(self, timeout: float = RECONNECT_TIMEOUT) -> WebRTCPeer:
        """After the current connection closed: wait for a new one."""
        if self.is_creator:
            self._spawn(self._renegotiate_soon())
        return await self._wait_ready(first=False, timeout=timeout)

    async def drop(self) -> None:
        """Abandon the current connection (it stalled), so a new one is made."""
        if self._peer is not None:
            await self._peer.close()

    async def close(self) -> None:
        self._closing = True
        tasks = [t for t in [self._signaling, *self._tasks] if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._peer is not None:
            await self._peer.close()
        if self._ws is not None:
            await self._ws.close()

    # ── Waiting ─────────────────────────────────────────────────────

    def _pulse(self) -> None:
        """Wake everything waiting in _wait_ready to re-check the state."""
        self._changed.set()
        self._changed = asyncio.Event()

    async def _wait_ready(self, *, first: bool, timeout: float | None = None) -> WebRTCPeer:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._ready is not None and not self._ready.closed:
                return self._ready
            if self._fatal is not None:
                raise self._fatal
            if first and deadline is None and self._negotiation_started is not None:
                deadline = self._negotiation_started + CONNECT_TIMEOUT
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                if self._last_ice_failed:
                    raise ConnectError(_DIRECT_FAILED)
                if first:
                    raise ConnectError("Timed out establishing the connection.")
                raise ConnectError("Couldn't reconnect to the other person.")
            changed = self._changed
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), remaining)

    # ── Signaling ───────────────────────────────────────────────────

    async def _signaling_loop(self) -> None:
        """Keep a signaling WebSocket open, reconnecting with our peer_id."""
        backoff = 0.5
        while not self._closing:
            url = self._ws_url + (f"?peer_id={self._peer_id}" if self._peer_id else "")
            reason, code = "", None
            try:
                async with websockets.connect(url) as ws:
                    self._ws = ws
                    backoff = 0.5
                    async for raw in ws:
                        await self._on_signal(json.loads(raw))
                    code, reason = ws.close_code, ws.close_reason or ""
            except ConnectionClosed as exc:
                if exc.rcvd is not None:
                    code, reason = exc.rcvd.code, exc.rcvd.reason
            except OSError as exc:
                if self._peer_id is None:  # never got in at all
                    self._set_fatal(f"Can't reach the signaling server at {self.server}: {exc}")
                    return
                log.debug("signaling unreachable: %s", exc)
            finally:
                self._ws = None

            if code == _POLICY_VIOLATION:
                # Room gone, expired or full: reconnecting can't help. A live
                # P2P connection is unaffected; it just can't be re-made.
                self._set_fatal(explain_close(reason, self.room_id))
                return
            if self._closing:
                return
            log.debug("signaling closed (%s %s); reconnecting in %.1fs", code, reason, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def _set_fatal(self, message: str) -> None:
        self._fatal = ConnectError(message)
        self._pulse()

    async def _send_signal(self, msg: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionError("signaling is down")
        await self._ws.send(json.dumps(msg))

    async def _on_signal(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "hello":
            self._peer_id = msg["peer_id"]
            log.debug("signaling connected (peer_id=%s)", self._peer_id)
            # Back on signaling with the P2P link down: try again now
            if self.is_creator and self._other_seen and not self._connected():
                self._spawn(self._negotiate())
        elif t == "peer_joined":
            self._other_seen = True
            if self.is_creator and not self._connected():
                log.info("Peer joined — connecting...")
                self._spawn(self._negotiate())
        elif t == "offer" and not self.is_creator:
            await self._accept_offer(msg)
        elif t == "answer" and self.is_creator and self._peer is not None:
            await self._peer.handle_signaling(msg)
        elif t in ("peer_disconnected", "peer_left"):
            log.debug("signaling: %s", t)
        elif t == "error":
            log.error("Signaling server: %s", msg.get("reason"))

    # ── WebRTC ──────────────────────────────────────────────────────

    def _connected(self) -> bool:
        return self._ready is not None and not self._ready.closed

    def _new_peer(self) -> WebRTCPeer:
        """Replace the current connection with a fresh one."""
        old = self._peer
        if old is not None:
            self._spawn(old.close())
        self._ready = None
        self._last_ice_failed = False
        self._negotiation_started = time.monotonic()
        self._peer = WebRTCPeer(on_signaling_message=self._send_signal, config=self._rtc_config)
        self._pulse()
        self._spawn(self._watch(self._peer))
        return self._peer

    async def _negotiate(self) -> None:
        """Creator: start a new connection with an offer."""
        if self._closing or self._ws is None:
            return  # retried on the next hello / peer_joined
        peer = self._new_peer()
        try:
            await peer.create_offer()
        except ConnectionError:
            log.debug("couldn't send offer; will retry when signaling is back")

    async def _renegotiate_soon(self, delay: float = 1.0) -> None:
        """Creator, after a drop: re-offer unless something else already did."""
        await asyncio.sleep(delay)
        if not self._connected() and (self._peer is None or self._peer.closed):
            await self._negotiate()

    async def _accept_offer(self, msg: dict[str, Any]) -> None:
        """Joiner: any offer replaces the current connection."""
        peer = self._new_peer()
        try:
            await peer.handle_signaling(msg)
        except ConnectionError:
            log.debug("couldn't send answer; waiting for a new offer")

    async def _watch(self, peer: WebRTCPeer) -> None:
        """Track one connection: publish it when open, notice when it ends."""
        try:
            await asyncio.wait_for(asyncio.shield(peer.channel_ready), CONNECT_TIMEOUT)
        except TimeoutError:
            if peer is self._peer:
                self._last_ice_failed = peer.pc.iceConnectionState == "failed"
                await peer.close()
                self._pulse()
                if self.is_creator and not self._closing:
                    await asyncio.sleep(2.0)  # then try again
                    if peer is self._peer:
                        await self._negotiate()
            return
        if peer is not self._peer:
            await peer.close()  # superseded while connecting
            return
        self._ready = peer
        self._pulse()

        await peer.wait_closed()
        if self._ready is peer:
            self._ready = None
            self._pulse()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
