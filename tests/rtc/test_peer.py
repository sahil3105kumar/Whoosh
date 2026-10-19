"""Test that two WebRTCPeers can connect and exchange data locally."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from packages.rtc.peer import HIGH_WATER_BYTES, WebRTCPeer


@pytest.fixture
async def connected_peers() -> AsyncIterator[tuple[WebRTCPeer, WebRTCPeer]]:
    """Two peers connected via in-memory signaling queues."""
    q_a2b: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    q_b2a: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    peer_a = WebRTCPeer(on_signaling_message=q_a2b.put)
    peer_b = WebRTCPeer(on_signaling_message=q_b2a.put)

    # Relay tasks — shuttle SDP between peers
    async def relay(src: asyncio.Queue[dict[str, Any]], dest: WebRTCPeer) -> None:
        while True:
            await dest.handle_signaling(await src.get())

    t1 = asyncio.create_task(relay(q_a2b, peer_b))
    t2 = asyncio.create_task(relay(q_b2a, peer_a))

    try:
        await peer_a.create_offer()
        await asyncio.wait_for(
            asyncio.gather(peer_a.channel_ready, peer_b.channel_ready), timeout=5.0
        )
        yield peer_a, peer_b
    finally:
        t1.cancel()
        t2.cancel()
        await peer_a.close()
        await peer_b.close()


async def test_datachannel_round_trip(connected_peers):
    peer_a, peer_b = connected_peers

    peer_a.send("ping")
    peer_b.send(b"pong")

    assert await asyncio.wait_for(peer_b.recv(), timeout=2.0) == "ping"
    assert await asyncio.wait_for(peer_a.recv(), timeout=2.0) == b"pong"


async def test_bulk_send_with_drain_arrives_in_order(connected_peers):
    peer_a, peer_b = connected_peers
    chunk = 16 * 1024
    count = (4 * HIGH_WATER_BYTES) // chunk  # enough to hit backpressure

    async def sender() -> None:
        for i in range(count):
            await peer_a.drain()
            assert peer_a.channel is not None
            assert peer_a.channel.bufferedAmount <= HIGH_WATER_BYTES + chunk
            peer_a.send(i.to_bytes(4, "big") * (chunk // 4))

    send_task = asyncio.create_task(sender())
    for i in range(count):
        data = await asyncio.wait_for(peer_b.recv(), timeout=5.0)
        assert isinstance(data, bytes)
        assert data[:4] == i.to_bytes(4, "big")
    await send_task


async def test_recv_returns_none_after_close(connected_peers):
    peer_a, peer_b = connected_peers

    await peer_a.close()

    assert await asyncio.wait_for(peer_b.recv(), timeout=5.0) is None
    with pytest.raises(ConnectionError):
        peer_b.send("too late")
