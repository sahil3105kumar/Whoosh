"""
Whoosh CLI — create or join a room and establish a P2P DataChannel.

Usage:
    python -m apps.cli.main create [--server URL]
    python -m apps.cli.main join ROOM_ID [--server URL]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import urllib.request
from typing import Any

import websockets
from aiortc import RTCConfiguration, RTCIceServer

from packages.rtc.peer import WebRTCPeer

logging.basicConfig(level=logging.INFO, format="%(message)s")
# aioice logs every ICE candidate-pair check at INFO — dozens of lines per connect
logging.getLogger("aioice").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

#: Seconds allowed from the start of negotiation to an open DataChannel.
#: Covers STUN candidate gathering plus ICE connectivity checks.
CONNECT_TIMEOUT = 30.0


# ── HTTP helper (stdlib only, no requests/httpx needed) ──────────────


def _create_room(server: str) -> str:
    req = urllib.request.Request(f"{server}/rooms", method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["room_id"]


def _fetch_rtc_config(server: str) -> RTCConfiguration:
    with urllib.request.urlopen(f"{server}/config") as resp:
        ice_servers = json.loads(resp.read())["ice_servers"]
    return RTCConfiguration(iceServers=[RTCIceServer(urls=s["urls"]) for s in ice_servers])


# ── Main loop ────────────────────────────────────────────────────────


async def _run(server: str, room_id: str, is_creator: bool) -> None:
    ws_url = f"{server.replace('http', 'ws')}/rooms/{room_id}/ws"
    rtc_config = _fetch_rtc_config(server)

    async with websockets.connect(ws_url) as ws:
        peer = WebRTCPeer(
            on_signaling_message=lambda msg: ws.send(json.dumps(msg)),
            on_message=lambda data: _on_data(data),
            config=rtc_config,
        )
        # Resolved once the other peer shows up (creator: peer_joined,
        # joiner: offer). Until then we wait indefinitely.
        negotiation_started: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def _listen_signaling() -> None:
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type")

                    if t in ("peer_joined", "offer") and not negotiation_started.done():
                        negotiation_started.set_result(None)

                    if t == "hello":
                        log.info("Connected (peer_id=%s)", msg["peer_id"])

                    elif t == "peer_joined":
                        if is_creator:
                            log.info("Peer joined — initiating WebRTC...")
                            await peer.create_offer()

                    elif t in ("offer", "answer"):
                        await peer.handle_signaling(msg)

                    elif t == "peer_disconnected":
                        log.warning("Peer dropped from signaling.")

                    elif t == "peer_left":
                        log.warning("Peer left the room.")

                    elif t == "error":
                        log.error("Signaling server: %s", msg.get("reason"))
            except websockets.exceptions.ConnectionClosed:
                pass

        listen_task = asyncio.create_task(_listen_signaling())

        try:
            if not await _wait_or_fail(negotiation_started, listen_task):
                log.error("Signaling closed before a peer arrived.")
                sys.exit(1)

            if not await _wait_or_fail(peer.channel_ready, listen_task, timeout=CONNECT_TIMEOUT):
                if peer.pc.iceConnectionState == "failed":
                    log.error(
                        "Direct connection failed: no network path between the peers. "
                        "One side is likely behind a strict NAT or firewall "
                        "(relay servers aren't supported yet)."
                    )
                elif listen_task.done():
                    log.error("Signaling closed before the DataChannel opened.")
                else:
                    log.error("Timed out waiting for DataChannel.")
                sys.exit(1)

            print("\n✓ P2P Connection Established!")

            assert peer.channel is not None
            peer.channel.send("hello from CLI")
            await asyncio.sleep(1)  # let the other side's message arrive

            if listen_task.done():
                listen_task.result()
        finally:
            listen_task.cancel()
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


async def _on_data(data: str | bytes) -> None:
    print(f"[peer] {data}")


# ── Entrypoint ───────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(description="Whoosh CLI")
    p.add_argument("--server", default="http://localhost:8000", help="Signaling server URL")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create", help="Create a room and wait for a peer")
    j = sub.add_parser("join", help="Join an existing room")
    j.add_argument("room_id")

    args = p.parse_args()

    if args.cmd == "create":
        room_id = _create_room(args.server)
        print(f"Room created: {room_id}")
        print("Waiting for peer to join...")
        asyncio.run(_run(args.server, room_id, is_creator=True))

    elif args.cmd == "join":
        print(f"Joining room {args.room_id}...")
        asyncio.run(_run(args.server, args.room_id, is_creator=False))


if __name__ == "__main__":
    main()
