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

import websockets

from packages.rtc.peer import WebRTCPeer

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# ── HTTP helper (stdlib only, no requests/httpx needed) ──────────────


def _create_room(server: str) -> str:
    req = urllib.request.Request(f"{server}/rooms", method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["room_id"]


# ── Main loop ────────────────────────────────────────────────────────


async def _run(server: str, room_id: str, is_creator: bool) -> None:
    ws_url = f"{server.replace('http', 'ws')}/rooms/{room_id}/ws"

    async with websockets.connect(ws_url) as ws:
        peer = WebRTCPeer(
            on_signaling_message=lambda msg: ws.send(json.dumps(msg)),
            on_message=lambda data: _on_data(data),
        )

        try:
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("type")

                if t == "hello":
                    log.info("Connected (peer_id=%s)", msg["peer_id"])

                elif t == "peer_joined":
                    if is_creator:
                        log.info("Peer joined — initiating WebRTC...")
                        await peer.create_offer()

                elif t in ("offer", "answer"):
                    await peer.handle_signaling(msg)

                    # After processing signaling, check if channel is ready
                    if peer.channel_ready.done():
                        break

                elif t == "peer_disconnected":
                    log.warning("Peer dropped from signaling.")

                # After every message, check if the channel came up
                if peer.channel_ready.done():
                    break

            # Wait for DataChannel (may already be done)
            await asyncio.wait_for(peer.channel_ready, timeout=10.0)
            print("\n✓ P2P Connection Established!")

            assert peer.channel is not None
            peer.channel.send("hello from CLI")
            await asyncio.sleep(1)  # let the other side's message arrive

        except TimeoutError:
            log.error("Timed out waiting for DataChannel.")
            sys.exit(1)
        finally:
            await peer.close()


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
