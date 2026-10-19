"""
Link (#19): two in-process peers against a real signaling server. The
connection is broken in different ways and must come back by itself.
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

from apps.cli.connection import ConnectError, Link, create_room
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


@pytest.fixture
async def links(server: str) -> AsyncIterator[tuple[Link, Link, WebRTCPeer, WebRTCPeer]]:
    room = await asyncio.to_thread(create_room, server)
    creator = Link(server, room, is_creator=True)
    joiner = Link(server, room, is_creator=False)
    a, b = await asyncio.wait_for(asyncio.gather(creator.open(), joiner.open()), 30)
    yield creator, joiner, a, b
    await creator.close()
    await joiner.close()


async def _talk(a: WebRTCPeer, b: WebRTCPeer) -> None:
    """The two peers can exchange a message both ways."""
    a.send("ping")
    assert await asyncio.wait_for(b.recv(), 10) == "ping"
    b.send("pong")
    assert await asyncio.wait_for(a.recv(), 10) == "pong"


async def _reconnect_both(creator: Link, joiner: Link) -> tuple[WebRTCPeer, WebRTCPeer]:
    return await asyncio.wait_for(asyncio.gather(creator.reconnect(), joiner.reconnect()), 45)


async def test_first_connection(links):
    _, _, a, b = links
    await _talk(a, b)


async def test_reconnects_after_the_connection_drops(links):
    creator, joiner, a, b = links
    await creator.drop()
    await asyncio.wait_for(b.wait_closed(), 30)

    a2, b2 = await _reconnect_both(creator, joiner)

    assert a2 is not a and b2 is not b
    await _talk(a2, b2)


async def test_joiner_side_drop_also_recovers(links):
    creator, joiner, a, b = links
    await joiner.drop()
    await asyncio.wait_for(a.wait_closed(), 30)

    a2, b2 = await _reconnect_both(creator, joiner)
    await _talk(a2, b2)


async def test_recovers_after_signaling_drops_too(links):
    creator, joiner, a, b = links
    assert joiner._ws is not None
    await joiner._ws.close()  # signaling drops; P2P is unaffected...
    await _talk(a, b)

    await creator.drop()  # ...until it drops as well
    a2, b2 = await _reconnect_both(creator, joiner)
    await _talk(a2, b2)


async def test_unknown_room_fails_fast(server: str):
    link = Link(server, "deadbeef", is_creator=False)
    with pytest.raises(ConnectError, match="doesn't exist"):
        await asyncio.wait_for(link.open(), 10)
    await link.close()
