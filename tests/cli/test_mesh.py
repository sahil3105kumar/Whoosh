"""
Mesh (#19, #23): in-process peers against a real signaling server.
Every pair must connect, and come back by itself when broken.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from apps.cli.connection import Fatal, Mesh, PeerConnected, create_room
from packages.rtc.peer import WebRTCPeer

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "WHOOSH_STUN_URLS": "", "WHOOSH_ROOM_CREATE_PER_MINUTE": "1000"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "apps.server.main:app", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while True:
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=1)
            break
        except OSError:
            if time.monotonic() > deadline:
                proc.kill()
                raise
            time.sleep(0.2)
    yield base
    proc.terminate()
    proc.wait(timeout=10)


class Node:
    """One participant: a Mesh plus the live connection to each other peer."""

    def __init__(self, server: str, room: str, password: str | None = None) -> None:
        self.mesh = Mesh(server, room, password=password)
        self.peers: dict[str, WebRTCPeer] = {}
        self._changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.mesh.start()
        self._task = asyncio.create_task(self._consume())

    async def _consume(self) -> None:
        while True:
            event = await self.mesh.events.get()
            if isinstance(event, PeerConnected):
                self.peers[event.peer_id] = event.peer
            self._changed.set()
            self._changed = asyncio.Event()

    async def connected_to(
        self, count: int, *, newer_than: dict[str, WebRTCPeer] | None = None
    ) -> None:
        """Wait for ``count`` live connections (all different from ``newer_than``)."""

        def ok() -> bool:
            live = [
                p
                for pid, p in self.peers.items()
                if not p.closed and (newer_than or {}).get(pid) is not p
            ]
            return len(live) >= count

        async def wait() -> None:
            while not ok():
                await self._changed.wait()

        await asyncio.wait_for(wait(), 45)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await self.mesh.close()


async def _talk(a: Node, b: Node) -> None:
    """a and b can exchange messages over their pair's connection."""
    assert a.mesh.peer_id and b.mesh.peer_id
    to_b, to_a = a.peers[b.mesh.peer_id], b.peers[a.mesh.peer_id]
    to_b.send(f"hi from {a.mesh.peer_id}")
    assert await asyncio.wait_for(to_a.recv(), 10) == f"hi from {a.mesh.peer_id}"
    to_a.send("hi back")
    assert await asyncio.wait_for(to_b.recv(), 10) == "hi back"


@pytest.fixture
async def room(server: str) -> str:
    return await asyncio.to_thread(create_room, server)


@pytest.fixture
async def nodes(server: str, room: str) -> AsyncIterator[list[Node]]:
    created: list[Node] = []
    yield created
    for node in created:
        await node.close()


async def _join(server: str, room: str, nodes: list[Node], count: int) -> list[Node]:
    new = [Node(server, room) for _ in range(count)]
    nodes.extend(new)
    for node in new:
        await node.start()
    return new


async def test_two_peers(server: str, room: str, nodes: list[Node]):
    a, b = await _join(server, room, nodes, 2)
    await asyncio.gather(a.connected_to(1), b.connected_to(1))
    await _talk(a, b)


async def test_three_peers_form_a_full_mesh(server: str, room: str, nodes: list[Node]):
    a, b, c = await _join(server, room, nodes, 3)
    await asyncio.gather(a.connected_to(2), b.connected_to(2), c.connected_to(2))
    await _talk(a, b)
    await _talk(a, c)
    await _talk(b, c)


async def test_a_dropped_pair_reconnects(server: str, room: str, nodes: list[Node]):
    a, b, c = await _join(server, room, nodes, 3)
    await asyncio.gather(a.connected_to(2), b.connected_to(2), c.connected_to(2))
    assert b.mesh.peer_id
    before_a, before_b = dict(a.peers), dict(b.peers)

    await a.mesh.drop(b.mesh.peer_id)  # break only the a–b pair

    await asyncio.gather(
        a.connected_to(1, newer_than=before_a), b.connected_to(1, newer_than=before_b)
    )
    await _talk(a, b)
    await _talk(a, c)  # untouched pair still fine


async def test_recovers_after_signaling_drops_too(server: str, room: str, nodes: list[Node]):
    a, b = await _join(server, room, nodes, 2)
    await asyncio.gather(a.connected_to(1), b.connected_to(1))
    assert b.mesh._ws is not None and b.mesh.peer_id
    await b.mesh._ws.close()  # signaling drops; P2P is unaffected...
    await _talk(a, b)

    before_a, before_b = dict(a.peers), dict(b.peers)
    await a.mesh.drop(b.mesh.peer_id)  # ...until it drops as well
    await asyncio.gather(
        a.connected_to(1, newer_than=before_a), b.connected_to(1, newer_than=before_b)
    )
    await _talk(a, b)


async def test_staggered_arrival(server: str, room: str, nodes: list[Node]):
    (a,) = await _join(server, room, nodes, 1)
    await asyncio.sleep(1.0)
    (b,) = await _join(server, room, nodes, 1)
    await asyncio.gather(a.connected_to(1), b.connected_to(1))
    await _talk(a, b)


async def test_unknown_room_is_fatal(server: str):
    mesh = Mesh(server, "deadbeef")
    await mesh.start()
    event = await asyncio.wait_for(mesh.events.get(), 10)
    assert isinstance(event, Fatal)
    assert "doesn't exist" in str(event.error)
    await mesh.close()


async def test_forgotten_peers_are_not_reconnected(server: str, room: str, nodes: list[Node]):
    a, b = await _join(server, room, nodes, 2)
    await asyncio.gather(a.connected_to(1), b.connected_to(1))
    assert a.mesh.peer_id and b.mesh.peer_id
    a.mesh.forget(b.mesh.peer_id)
    b.mesh.forget(a.mesh.peer_id)
    await asyncio.sleep(3)
    assert a.mesh.connected_peers() == [] and b.mesh.connected_peers() == []


async def test_password_protected_room(server: str, nodes: list[Node]):
    room = await asyncio.to_thread(lambda: create_room(server, password="s3cret"))
    a, b = Node(server, room, "s3cret"), Node(server, room, "s3cret")
    nodes.extend([a, b])
    await a.start()
    await b.start()
    await asyncio.gather(a.connected_to(1), b.connected_to(1))
    await _talk(a, b)

    intruder = Mesh(server, room, password="guess")
    await intruder.start()
    event = await asyncio.wait_for(intruder.events.get(), 10)
    assert isinstance(event, Fatal)
    assert "Wrong password" in str(event.error)
    await intruder.close()
