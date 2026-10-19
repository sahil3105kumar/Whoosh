"""
TURN relay fallback (#31): peers forced to relay-only still connect, with
a real coturn and the server's short-lived credentials. Skipped without
Docker (CI has it).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="install the interop dependency group")
from playwright.async_api import Browser, Page  # noqa: E402

from tests.interop.test_browser_cli import (  # noqa: E402
    CAPTURE_PCS,
    ROOT,
    TIMEOUT_S,
    WEB_DIST,
    Cli,
    browser,  # noqa: F401  (fixture)
    create_room,
    drop_file,
    pattern_bytes,
    received_sha256,
    wait_connected,
)

pytestmark = pytest.mark.interop

TURN_PORT = 3479
TURN_SECRET = "whoosh-test-secret"
CONTAINER = "whoosh-turn-pytest"


@pytest.fixture(scope="module")
def turn() -> Iterator[str]:
    if shutil.which("docker") is None:
        pytest.skip("Docker is needed to run a TURN server")
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    started = subprocess.run(
        [
            "docker", "run", "-d", "--name", CONTAINER, "--network", "host",
            "coturn/coturn:latest", "-n", "--log-file=stdout",
            f"--listening-port={TURN_PORT}", "--listening-ip=127.0.0.1",
            "--relay-ip=127.0.0.1", "--min-port=49300", "--max-port=49400",
            "--use-auth-secret", f"--static-auth-secret={TURN_SECRET}",
            "--realm=whoosh.test", "--no-tls", "--allow-loopback-peers",
            "--fingerprint",
        ],
        capture_output=True,
        text=True,
    )  # fmt: skip
    if started.returncode != 0:
        pytest.skip(f"couldn't start coturn: {started.stderr.strip()[:200]}")
    time.sleep(1.5)
    yield f"turn:127.0.0.1:{TURN_PORT}?transport=udp"
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


@pytest.fixture(scope="module")
def relay_server(turn: str) -> Iterator[str]:
    """A signaling server that hands out TURN credentials and makes
    browsers use only the relay."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {
        **os.environ,
        "WHOOSH_WEB_DIR": str(WEB_DIST),
        "WHOOSH_STUN_URLS": "",
        "WHOOSH_TURN_URLS": turn,
        "WHOOSH_TURN_SECRET": TURN_SECRET,
        "WHOOSH_ICE_TRANSPORT_POLICY": "relay",
    }
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


async def selected_candidate_type(page: Page) -> str:
    """The local candidate type of the page's active connection."""
    kind: str = await page.evaluate(
        """async () => {
            const pc = window.__pcs.at(-1);
            const stats = [...(await pc.getStats()).values()];
            const pair = stats.find(
                (s) => s.type === "candidate-pair" && s.nominated && s.state === "succeeded"
            );
            return stats.find((s) => s.id === pair.localCandidateId).candidateType;
        }"""
    )
    return kind


async def test_browsers_connect_through_the_relay(relay_server: str, browser: Browser):  # noqa: F811
    alice = await browser.new_page()
    bob = await browser.new_page()
    for page in (alice, bob):
        await page.add_init_script(CAPTURE_PCS)
    room = await create_room(alice, relay_server)
    await bob.goto(f"{relay_server}/#{room}")
    await wait_connected(alice)
    await wait_connected(bob)

    assert await selected_candidate_type(alice) == "relay"
    assert await selected_candidate_type(bob) == "relay"

    await drop_file(alice, "relayed.bin", 1_000_000, seed=4)
    expected = hashlib.sha256(pattern_bytes(1_000_000, seed=4)).hexdigest()
    assert await received_sha256(bob, "relayed.bin") == expected


async def test_cli_reaches_a_relay_only_browser(
    relay_server: str,
    browser: Browser,  # noqa: F811
    tmp_path: Path,
):
    page = await browser.new_page()
    await page.add_init_script(CAPTURE_PCS)
    room = await create_room(page, relay_server)
    cli = await Cli.start(relay_server, "receive", room, "--out", str(tmp_path))
    try:
        await wait_connected(page)
        # The browser only offers relay candidates, so this path goes via TURN
        assert await selected_candidate_type(page) == "relay"
        await drop_file(page, "relayed.bin", 1_000_000, seed=6)
        await page.get_by_text("Delivered").wait_for(timeout=TIMEOUT_S * 1000)
        await page.get_by_role("button", name="Leave").click()
        assert await cli.exit_code() == 0, cli.output()
        assert (tmp_path / "relayed.bin").read_bytes() == pattern_bytes(1_000_000, seed=6)
    finally:
        await cli.close()
