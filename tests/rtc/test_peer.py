"""Test that two WebRTCPeers can connect and exchange data locally."""

from __future__ import annotations

import asyncio

from packages.rtc.peer import WebRTCPeer


async def test_datachannel_round_trip():
    """Two peers connect via in-memory signaling queues, send a message each way."""
    received_a: list[str | bytes] = []
    received_b: list[str | bytes] = []

    q_a2b: asyncio.Queue[dict] = asyncio.Queue()
    q_b2a: asyncio.Queue[dict] = asyncio.Queue()

    peer_a = WebRTCPeer(on_signaling_message=q_a2b.put, on_message=lambda m: _append(received_a, m))
    peer_b = WebRTCPeer(on_signaling_message=q_b2a.put, on_message=lambda m: _append(received_b, m))

    # Relay tasks — shuttle SDP between peers
    async def relay(src: asyncio.Queue[dict], dest: WebRTCPeer) -> None:
        while True:
            await dest.handle_signaling(await src.get())

    t1 = asyncio.create_task(relay(q_a2b, peer_b))
    t2 = asyncio.create_task(relay(q_b2a, peer_a))

    try:
        await peer_a.create_offer()
        await asyncio.wait_for(
            asyncio.gather(peer_a.channel_ready, peer_b.channel_ready), timeout=5.0
        )

        assert peer_a.channel is not None
        assert peer_b.channel is not None

        peer_a.channel.send("ping")
        peer_b.channel.send("pong")
        await asyncio.sleep(0.2)

        assert "ping" in received_b
        assert "pong" in received_a
    finally:
        t1.cancel()
        t2.cancel()
        await peer_a.close()
        await peer_b.close()


async def _append(lst: list, item: str | bytes) -> None:
    lst.append(item)
