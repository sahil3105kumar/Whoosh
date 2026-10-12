"""
Smoke test a running signaling server over real HTTP and WebSockets.

Catches what the in-process tests can't: packaging and deployment
problems such as a missing WebSocket library in the image or a proxy
that doesn't forward upgrades.

    uv run python scripts/smoke_test.py http://localhost:8000
    SSL_CERT_FILE=root.crt uv run python scripts/smoke_test.py https://localhost:8443
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

import websockets


def _http(method: str, url: str) -> dict[str, object]:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        result: dict[str, object] = json.loads(resp.read())
    return result


async def _recv(ws: websockets.ClientConnection) -> dict[str, object]:
    msg: dict[str, object] = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    return msg


async def main(base: str) -> None:
    assert _http("GET", f"{base}/healthz")["status"] == "ok"
    print("✓ healthz")

    config = _http("GET", f"{base}/config")
    assert isinstance(config["ice_servers"], list)
    print("✓ config")

    room_id = _http("POST", f"{base}/rooms")["room_id"]
    print(f"✓ room created ({room_id})")

    ws_url = f"{base.replace('http', 'ws', 1)}/rooms/{room_id}/ws"
    async with websockets.connect(ws_url) as a:
        assert (await _recv(a))["type"] == "hello"
        async with websockets.connect(ws_url) as b:
            assert (await _recv(b))["type"] == "hello"
            assert (await _recv(a))["type"] == "peer_joined"
            print("✓ two peers joined over WebSocket")

            await a.send(json.dumps({"type": "offer", "sdp": "smoke"}))
            relayed = await _recv(b)
            assert relayed == {"type": "offer", "sdp": "smoke"}, relayed
            print("✓ signaling relayed")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1].rstrip("/")))
    print("Smoke test passed.")
