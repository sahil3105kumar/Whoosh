"""
Signaling + WebRTC for the CLI: one room, any number of peers (#23).

``Mesh`` owns the signaling WebSocket and a WebRTC connection to every
other peer in the room (a full mesh, docs/decisions.md D55). Each pair
reconnects on its own when its connection drops (D50): in every pair
the peer with the smaller peer_id makes the offers, so the rule needs
no coordination and survives reconnects. A dropped WebSocket reconnects
with its peer_id so it keeps its slot in the room.

The application consumes ``Mesh.events``: a peer connected (new, or
back after a drop), a peer is gone, a peer couldn't be reached, or the
room itself became unusable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
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

#: How long a peer whose connection dropped (without saying bye) is given
#: to come back before it's treated as gone.
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
        return f"Room {room_id} is full."
    if reason.startswith("Wrong password"):
        return f"Wrong password for room {room_id}."
    if reason.startswith("This room needs a password"):
        return f"Room {room_id} needs a password (--password)."
    return reason


def create_room(server: str, *, password: str | None = None) -> str:
    body = json.dumps({"password": password}).encode() if password else None
    req = urllib.request.Request(
        f"{server}/rooms",
        method="POST",
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
    )
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


def room_needs_password(server: str, room_id: str) -> bool:
    """Look a room up: whether joining needs a password. Raises ConnectError
    if it doesn't exist."""
    try:
        with urllib.request.urlopen(f"{server}/rooms/{room_id}") as resp:
            return bool(json.loads(resp.read()).get("password_required"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ConnectError(explain_close("No such room", room_id)) from exc
        raise


def _fetch_rtc_config(server: str) -> RTCConfiguration:
    with urllib.request.urlopen(f"{server}/config") as resp:
        ice_servers = json.loads(resp.read())["ice_servers"]
    # TURN entries carry short-lived credentials (#31); aiortc can't be forced
    # to relay-only, so the server's ice_transport_policy only applies to
    # browsers
    return RTCConfiguration(
        iceServers=[
            RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
            for s in ice_servers
        ]
    )


DIRECT_FAILED = (
    "Direct connection failed: no network path between the peers. "
    "One side is likely behind a strict NAT or firewall, and the server's "
    "TURN relay is missing or unreachable."
)


# ── Events ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PeerConnected:
    """An open connection to ``peer_id``: new, or replacing one that dropped."""

    peer_id: str
    peer: WebRTCPeer


@dataclass(frozen=True)
class PeerGone:
    """``peer_id`` left the room for good (its slot was released)."""

    peer_id: str


@dataclass(frozen=True)
class PeerUnreachable:
    """Connecting to ``peer_id`` timed out; the offering side keeps retrying."""

    peer_id: str
    message: str


@dataclass(frozen=True)
class Fatal:
    """The room can't be used any more (gone, full, server unreachable)."""

    error: ConnectError


MeshEvent = PeerConnected | PeerGone | PeerUnreachable | Fatal


# ── Mesh ────────────────────────────────────────────────────────────


@dataclass(eq=False)
class _Pair:
    """Our connection to one other peer."""

    peer_id: str
    #: The current WebRTC connection (maybe still negotiating)...
    peer: WebRTCPeer | None = None
    #: ...and the same object once its DataChannel is open.
    ready: WebRTCPeer | None = None
    retry: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def connected(self) -> bool:
        return self.ready is not None and not self.ready.closed


class Mesh:
    def __init__(self, server: str, room_id: str, *, password: str | None = None) -> None:
        self.server = server
        self.room_id = room_id
        self._password = password
        self.events: asyncio.Queue[MeshEvent] = asyncio.Queue()
        self._ws_url = f"{server.replace('http', 'ws', 1)}/rooms/{room_id}/ws"
        self._rtc_config: RTCConfiguration | None = None
        self._ws: ClientConnection | None = None
        #: Our slot in the room, reused when the WebSocket reconnects.
        self.peer_id: str | None = None
        self._pairs: dict[str, _Pair] = {}
        #: Peers that said bye: don't reconnect to them.
        self._forgotten: set[str] = set()
        self._closing = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._signaling: asyncio.Task[None] | None = None

    # ── Public API ──────────────────────────────────────────────────

    async def start(self) -> None:
        self._rtc_config = await asyncio.to_thread(_fetch_rtc_config, self.server)
        self._signaling = asyncio.create_task(self._signaling_loop())

    def connected_peers(self) -> list[str]:
        return [pid for pid, pair in self._pairs.items() if pair.connected]

    async def drop(self, peer_id: str) -> None:
        """Abandon the connection to ``peer_id`` (it stalled), so a new one
        is made."""
        pair = self._pairs.get(peer_id)
        if pair is not None and pair.peer is not None:
            await pair.peer.close()

    def forget(self, peer_id: str) -> None:
        """``peer_id`` left on purpose: stop trying to reach it."""
        self._forgotten.add(peer_id)
        pair = self._pairs.pop(peer_id, None)
        if pair is not None:
            if pair.retry is not None:
                pair.retry.cancel()
            if pair.peer is not None:
                self._spawn(pair.peer.close())

    async def close(self) -> None:
        self._closing = True
        tasks = [t for t in [self._signaling, *self._tasks] if t is not None]
        tasks += [p.retry for p in self._pairs.values() if p.retry is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for pair in self._pairs.values():
            if pair.peer is not None:
                await pair.peer.close()
        if self._ws is not None:
            await self._ws.close()

    # ── Signaling ───────────────────────────────────────────────────

    async def _signaling_loop(self) -> None:
        """Keep a signaling WebSocket open, reconnecting with our peer_id."""
        backoff = 0.5
        while not self._closing:
            url = self._ws_url + (f"?peer_id={self.peer_id}" if self.peer_id else "")
            reason, code = "", None
            try:
                async with websockets.connect(url) as ws:
                    if self._password is not None:
                        # Protected room: the first message must authenticate
                        await ws.send(json.dumps({"type": "auth", "password": self._password}))
                    self._ws = ws
                    backoff = 0.5
                    async for raw in ws:
                        await self._on_signal(json.loads(raw))
                    code, reason = ws.close_code, ws.close_reason or ""
            except ConnectionClosed as exc:
                if exc.rcvd is not None:
                    code, reason = exc.rcvd.code, exc.rcvd.reason
            except OSError as exc:
                if self.peer_id is None:  # never got in at all
                    self._fatal(f"Can't reach the signaling server at {self.server}: {exc}")
                    return
                log.debug("signaling unreachable: %s", exc)
            finally:
                self._ws = None

            if code == _POLICY_VIOLATION:
                # Room gone, expired or full: reconnecting can't help. Live
                # P2P connections are unaffected; they just can't be re-made.
                self._fatal(explain_close(reason, self.room_id))
                return
            if self._closing:
                return
            log.debug("signaling closed (%s %s); reconnecting in %.1fs", code, reason, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def _fatal(self, message: str) -> None:
        self.events.put_nowait(Fatal(ConnectError(message)))

    async def _send_signal(self, to: str, msg: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionError("signaling is down")
        await self._ws.send(json.dumps({**msg, "to": to}))

    async def _on_signal(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        sender = msg.get("from")
        if t == "hello":
            self.peer_id = msg["peer_id"]
            log.debug("signaling connected (peer_id=%s)", self.peer_id)
            for other in msg.get("peers", []):
                self._learn(other)
            # Back on signaling: retry every pair that's down
            for pair in list(self._pairs.values()):
                if not pair.connected:
                    self._maybe_offer(pair)
        elif t == "peer_joined":
            self._learn(msg["peer_id"])
        elif t == "peer_left":
            self._lose(msg["peer_id"])
        elif t == "offer" and isinstance(sender, str):
            await self._accept_offer(sender, msg)
        elif t == "answer" and isinstance(sender, str):
            answered = self._pairs.get(sender)
            if answered is not None and answered.peer is not None:
                await answered.peer.handle_signaling(msg)
        elif t == "peer_disconnected":
            log.debug("signaling: %s dropped", msg.get("peer_id"))
        elif t == "error":
            log.debug("signaling server: %s", msg.get("reason"))

    def _learn(self, peer_id: str) -> None:
        """A peer is in the room: make sure we're connected to it."""
        if peer_id == self.peer_id or peer_id in self._forgotten:
            return
        pair = self._pairs.setdefault(peer_id, _Pair(peer_id))
        if not pair.connected:
            self._maybe_offer(pair)

    def _lose(self, peer_id: str) -> None:
        pair = self._pairs.pop(peer_id, None)
        if pair is not None:
            if pair.retry is not None:
                pair.retry.cancel()
            self.events.put_nowait(PeerGone(peer_id))

    # ── WebRTC ──────────────────────────────────────────────────────

    def _offers_to(self, pair: _Pair) -> bool:
        """In each pair the smaller peer_id offers (D55)."""
        return self.peer_id is not None and self.peer_id < pair.peer_id

    def _maybe_offer(self, pair: _Pair, delay: float = 0.0) -> None:
        if not self._offers_to(pair) or self._closing:
            return
        if pair.retry is not None and not pair.retry.done():
            return  # one already scheduled
        pair.retry = asyncio.create_task(self._offer_after(pair, delay))

    async def _offer_after(self, pair: _Pair, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        if self._closing or self._ws is None or pair.connected:
            return  # signaling down: retried on the next hello
        if self._pairs.get(pair.peer_id) is not pair:
            return  # gone meanwhile
        peer = self._new_peer(pair)
        try:
            await peer.create_offer()
        except ConnectionError:
            log.debug("couldn't send offer to %s; will retry", pair.peer_id)

    async def _accept_offer(self, sender: str, msg: dict[str, Any]) -> None:
        """Any offer from ``sender`` replaces our connection to it."""
        if sender in self._forgotten:
            return
        pair = self._pairs.setdefault(sender, _Pair(sender))
        peer = self._new_peer(pair)
        try:
            await peer.handle_signaling(msg)
        except ConnectionError:
            log.debug("couldn't answer %s; waiting for a new offer", sender)

    def _new_peer(self, pair: _Pair) -> WebRTCPeer:
        """Replace ``pair``'s connection with a fresh one."""
        if pair.peer is not None:
            self._spawn(pair.peer.close())
        pair.ready = None
        target = pair.peer_id
        pair.peer = WebRTCPeer(
            on_signaling_message=lambda msg: self._send_signal(target, msg),
            config=self._rtc_config,
        )
        self._spawn(self._watch(pair, pair.peer))
        return pair.peer

    async def _watch(self, pair: _Pair, peer: WebRTCPeer) -> None:
        """Track one connection: publish it when open, notice when it ends."""
        try:
            await asyncio.wait_for(asyncio.shield(peer.channel_ready), CONNECT_TIMEOUT)
        except TimeoutError:
            if peer is pair.peer:
                ice_failed = peer.pc.iceConnectionState == "failed"
                await peer.close()
                self.events.put_nowait(
                    PeerUnreachable(
                        pair.peer_id,
                        DIRECT_FAILED if ice_failed else "Timed out connecting.",
                    )
                )
                self._maybe_offer(pair, delay=2.0)  # keep trying
            return
        if peer is not pair.peer or self._pairs.get(pair.peer_id) is not pair:
            await peer.close()  # superseded while connecting
            return
        pair.ready = peer
        self.events.put_nowait(PeerConnected(pair.peer_id, peer))

        await peer.wait_closed()
        if pair.ready is peer:
            pair.ready = None
            # Dropped without a bye (the app forgets peers that say bye):
            # the offering side tries again shortly
            self._maybe_offer(pair, delay=1.0)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
