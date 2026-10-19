"""
Browser <-> CLI interop: a real signaling server, the built web client in
headless Chromium (Playwright), and the CLI as a subprocess.

The only tests that exercise the Python and TypeScript implementations
against each other (docs/decisions.md D41). Skipped unless the interop
dependency group, a Playwright Chromium and a web build are present:

    uv sync --group interop && uv run playwright install chromium
    (cd apps/web && npm run build)
    uv run pytest tests/interop
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="install the interop dependency group")
from playwright.async_api import Browser, Page, async_playwright  # noqa: E402
from playwright.async_api import Error as PlaywrightError  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WEB_DIST = ROOT / "apps/web/dist"

if not (WEB_DIST / "index.html").exists():
    pytest.skip("web client not built (cd apps/web && npm run build)", allow_module_level=True)

pytestmark = pytest.mark.interop

#: Generous: covers connection setup plus the transfer on a slow CI runner.
TIMEOUT_S = 60


# ── Fixtures ────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    port = _free_port()
    env = {
        **os.environ,
        "WHOOSH_WEB_DIR": str(WEB_DIST),
        # Peers share a machine: host candidates suffice, and skipping STUN
        # avoids slow gathering on hosts with many network interfaces
        "WHOOSH_STUN_URLS": "",
        "WHOOSH_ROOM_CREATE_PER_MINUTE": "1000",
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


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        try:
            b = await pw.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not available: {exc}")
        yield b
        await b.close()


# ── CLI subprocess ──────────────────────────────────────────────────


class Cli:
    """The whoosh CLI as a subprocess. Its output is drained continuously:
    with -v it logs every SCTP chunk, and an unread pipe fills up and
    blocks the CLI's event loop mid-transfer."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.lines: list[str] = []
        self._new_output = asyncio.Event()
        self._reader = asyncio.create_task(self._drain())

    @classmethod
    async def start(cls, server: str, *args: str) -> Cli:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "apps.cli.main",
            "--server",
            server,
            "-v",
            *args,
            cwd=ROOT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        return cls(proc)

    async def _drain(self) -> None:
        assert self.proc.stdout is not None
        while raw := await self.proc.stdout.readline():
            self.lines.append(raw.decode(errors="replace").rstrip())
            self._new_output.set()
        self._new_output.set()  # EOF

    async def wait_for(self, pattern: str) -> re.Match[str]:
        """Wait until an output line matches ``pattern``."""

        async def search() -> re.Match[str]:
            checked = 0
            while True:
                for line in self.lines[checked:]:
                    if m := re.search(pattern, line):
                        return m
                checked = len(self.lines)
                if self._reader.done():
                    raise AssertionError(f"CLI exited before printing {pattern!r}")
                self._new_output.clear()
                await self._new_output.wait()

        try:
            return await asyncio.wait_for(search(), TIMEOUT_S)
        except (TimeoutError, AssertionError) as exc:
            raise AssertionError(f"{exc}\n--- CLI output ---\n{self.output()}") from None

    async def type(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(f"{line}\n".encode())
        await self.proc.stdin.drain()

    async def exit_code(self) -> int:
        code = await asyncio.wait_for(self.proc.wait(), TIMEOUT_S)
        await self._reader
        return code

    def output(self) -> str:
        # Leave out per-packet transport noise and raw signaling frames
        noise = ("CandidatePair", "RTCSctpTransport", "< TEXT", "> TEXT", "a=")
        return "\n".join(line for line in self.lines if not any(n in line for n in noise))

    async def close(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()
        await self.proc.wait()
        await self._reader


# ── Browser helpers ─────────────────────────────────────────────────


def pattern_bytes(size: int, seed: int) -> bytes:
    """Deterministic content both sides can generate: (i*31 + seed) % 256."""
    period = bytes((i * 31 + seed) % 256 for i in range(256))  # repeats every 256 bytes
    return (period * (size // 256 + 1))[:size]


async def create_room(page: Page, server: str) -> str:
    await page.goto(server)
    await page.get_by_role("button", name="Create room").click()
    link = await page.get_by_label("Invite link").input_value(timeout=TIMEOUT_S * 1000)
    return link.rsplit("#", 1)[1]


async def wait_connected(page: Page) -> None:
    await page.get_by_text("Connected peer-to-peer").wait_for(timeout=TIMEOUT_S * 1000)


async def drop_file(
    page: Page, name: str, size: int, seed: int, last_modified: int = 1_000
) -> None:
    """Drop a generated file on the drop zone, as a user dragging it in would.
    A fixed ``last_modified`` makes re-drops of the same file resumable."""
    # The zone accepts files only once the peer's hello has arrived
    await page.get_by_text("Drop files here").wait_for(timeout=TIMEOUT_S * 1000)
    await page.evaluate(
        """([name, size, seed, lastModified]) => {
            const bytes = new Uint8Array(size);
            for (let i = 0; i < size; i++) bytes[i] = (i * 31 + seed) % 256;
            const dt = new DataTransfer();
            dt.items.add(new File([bytes], name, { lastModified }));
            const zone = [...document.querySelectorAll("div")]
                .find((d) => d.innerText?.startsWith("Drop files here"));
            zone.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true }));
        }""",
        [name, size, seed, last_modified],
    )


async def wait_progress(page: Page, percent: int) -> None:
    """Wait until a transfer row shows at least ``percent``% done."""
    await page.wait_for_function(
        """(min) => [...document.body.innerText.matchAll(/(\\d+)% ·/g)]
                .some((m) => Number(m[1]) >= min)""",
        arg=percent,
        timeout=TIMEOUT_S * 1000,
    )


async def received_sha256(page: Page, name: str) -> str:
    """SHA-256 of a file the browser received, from its Save link."""
    link = page.locator(f'a[download="{name}"]')
    await link.wait_for(timeout=TIMEOUT_S * 1000)
    digest: str = await link.evaluate(
        """async (a) => {
            const buf = await (await fetch(a.href)).arrayBuffer();
            const hash = await crypto.subtle.digest("SHA-256", buf);
            return [...new Uint8Array(hash)].map((b) => b.toString(16).padStart(2, "0")).join("");
        }"""
    )
    return digest


# ── Tests ───────────────────────────────────────────────────────────


async def test_browser_creates_cli_receives(server: str, browser: Browser, tmp_path: Path):
    page = await browser.new_page()
    room = await create_room(page, server)
    cli = await Cli.start(server, "receive", room, "--out", str(tmp_path))
    try:
        await wait_connected(page)
        await drop_file(page, "from-browser.bin", 2_000_000, seed=1)
        await page.get_by_text("Delivered").wait_for(timeout=TIMEOUT_S * 1000)
        await page.get_by_role("button", name="Leave").click()  # receive exits on close

        assert await cli.exit_code() == 0, cli.output()
        assert (tmp_path / "from-browser.bin").read_bytes() == pattern_bytes(2_000_000, seed=1)
    finally:
        await cli.close()


@pytest.mark.parametrize("run", range(5))  # repeated: probes open issue I1
async def test_cli_creates_browser_receives(
    server: str, browser: Browser, tmp_path: Path, run: int
):
    src = tmp_path / f"from-cli-{run}.bin"
    src.write_bytes(pattern_bytes(1_500_000, seed=run))
    cli = await Cli.start(server, "send", str(src))
    page = await browser.new_page()
    try:
        room = (await cli.wait_for(r"Room created: ([0-9a-f]{8})"))[1]
        await page.goto(f"{server}/#{room}")

        digest = await received_sha256(page, src.name)

        assert digest == hashlib.sha256(src.read_bytes()).hexdigest()
        assert await cli.exit_code() == 0, cli.output()
    finally:
        await cli.close()


async def test_chat_and_ping_both_ways(server: str, browser: Browser, tmp_path: Path):
    page = await browser.new_page()
    room = await create_room(page, server)
    cli = await Cli.start(server, "join", room, "--out", str(tmp_path))
    try:
        await cli.wait_for("Connected peer-to-peer")
        await wait_connected(page)

        await cli.type("hello from the CLI")
        await page.get_by_text("hello from the CLI").wait_for(timeout=TIMEOUT_S * 1000)

        await page.get_by_label("Message").fill("hello from the browser")
        await page.get_by_label("Message").press("Enter")
        await cli.wait_for("peer: hello from the browser")

        await cli.type("/ping")
        await cli.wait_for(r"pong: [\d.]+ ms")
        await page.get_by_role("button", name="Ping").click()
        await page.get_by_text("Round trip:").wait_for(timeout=TIMEOUT_S * 1000)

        await cli.type("/quit")
        assert await cli.exit_code() == 0, cli.output()
        await page.get_by_text("The other person left").wait_for(timeout=TIMEOUT_S * 1000)
    finally:
        await cli.close()


async def test_browser_to_browser(server: str, browser: Browser):
    alice = await browser.new_page()
    bob = await browser.new_page()
    room = await create_room(alice, server)
    await bob.goto(f"{server}/#{room}")
    await wait_connected(alice)
    await wait_connected(bob)

    await drop_file(alice, "between-browsers.bin", 3_000_000, seed=7)

    expected = hashlib.sha256(pattern_bytes(3_000_000, seed=7)).hexdigest()
    assert await received_sha256(bob, "between-browsers.bin") == expected


# ── Resume (#17) ────────────────────────────────────────────────────


async def test_browser_to_cli_resumes_after_page_closes(
    server: str, browser: Browser, tmp_path: Path
):
    size = 40_000_000
    page = await browser.new_page()
    room = await create_room(page, server)
    cli = await Cli.start(server, "receive", room, "--out", str(tmp_path))
    try:
        await drop_file(page, "big.bin", size, seed=3)
        await wait_progress(page, 20)
        # Close the tab mid-transfer. Playwright skips unload handlers, so
        # fire pagehide as a real browser would: the page says bye
        await page.evaluate("dispatchEvent(new PageTransitionEvent('pagehide'))")
        await asyncio.sleep(0.3)  # a real tab takes this long to tear down anyway
        await page.close()
        assert await cli.exit_code() == 1, cli.output()
        assert list((tmp_path / ".whoosh-partial").glob("*.part")), "partial was not kept"
    finally:
        await cli.close()

    page = await browser.new_page()
    room = await create_room(page, server)
    cli = await Cli.start(server, "receive", room, "--out", str(tmp_path))
    try:
        await drop_file(page, "big.bin", size, seed=3)  # same file again
        await page.get_by_text("Delivered").wait_for(timeout=TIMEOUT_S * 1000)
        await page.get_by_role("button", name="Leave").click()
        assert await cli.exit_code() == 0, cli.output()
        assert "resuming from" in cli.output()
        assert (tmp_path / "big.bin").read_bytes() == pattern_bytes(size, seed=3)
        assert not (tmp_path / ".whoosh-partial").exists()
    finally:
        await cli.close()


async def test_cli_to_browser_resumes_after_cli_is_killed(
    server: str, browser: Browser, tmp_path: Path
):
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(120_000_000))
    page = await browser.new_page()

    cli = await Cli.start(server, "send", str(src))
    try:
        room = (await cli.wait_for(r"Room created: ([0-9a-f]{8})"))[1]
        await page.goto(f"{server}/#{room}")
        await wait_progress(page, 20)
        cli.proc.kill()  # the sender vanishes mid-transfer
        # No bye arrived, so the browser assumes a network problem and tries
        # to get the connection back (#19)
        await page.get_by_text("reconnecting").wait_for(timeout=TIMEOUT_S * 1000)
    finally:
        await cli.close()

    # Same tab, new room: the browser still holds the partial in memory
    cli = await Cli.start(server, "send", str(src))
    try:
        room = (await cli.wait_for(r"Room created: ([0-9a-f]{8})"))[1]
        await page.goto(f"{server}/#{room}")
        digest = await received_sha256(page, src.name)
        assert digest == hashlib.sha256(src.read_bytes()).hexdigest()
        assert await cli.exit_code() == 0, cli.output()
        assert "resuming from" in cli.output()
        assert await page.get_by_text("resumed from").count() >= 1
    finally:
        await cli.close()


# ── Automatic reconnection (#19) ────────────────────────────────────

#: Keeps every RTCPeerConnection the page creates, so a test can cut one.
CAPTURE_PCS = """
window.__pcs = [];
const Original = RTCPeerConnection;
window.RTCPeerConnection = function (...args) {
  const pc = new Original(...args);
  window.__pcs.push(pc);
  return pc;
};
window.RTCPeerConnection.prototype = Original.prototype;
"""


async def cut_connection(page: Page) -> None:
    """Kill the page's live peer-to-peer connection, as a network failure would."""
    await page.evaluate("window.__pcs.at(-1).close()")


async def test_cli_send_survives_a_dropped_connection(
    server: str, browser: Browser, tmp_path: Path
):
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(120_000_000))
    page = await browser.new_page()
    await page.add_init_script(CAPTURE_PCS)

    cli = await Cli.start(server, "send", str(src))
    try:
        room = (await cli.wait_for(r"Room created: ([0-9a-f]{8})"))[1]
        await page.goto(f"{server}/#{room}")
        await wait_progress(page, 20)
        await cut_connection(page)

        # Nobody does anything: both sides reconnect and the send resumes
        digest = await received_sha256(page, src.name)
        assert digest == hashlib.sha256(src.read_bytes()).hexdigest()
        assert await cli.exit_code() == 0, cli.output()
        assert "Reconnected" in cli.output()
        assert "resuming from" in cli.output()
    finally:
        await cli.close()


async def test_browser_send_survives_a_dropped_connection(
    server: str, browser: Browser, tmp_path: Path
):
    size = 40_000_000
    page = await browser.new_page()
    await page.add_init_script(CAPTURE_PCS)
    room = await create_room(page, server)
    cli = await Cli.start(server, "receive", room, "--out", str(tmp_path))
    try:
        await drop_file(page, "big.bin", size, seed=5)
        await wait_progress(page, 20)
        await cut_connection(page)

        await page.get_by_text("Delivered").wait_for(timeout=TIMEOUT_S * 1000)
        await page.get_by_role("button", name="Leave").click()  # says bye
        assert await cli.exit_code() == 0, cli.output()
        assert "Reconnected" in cli.output()
        assert "resuming from" in cli.output()
        assert (tmp_path / "big.bin").read_bytes() == pattern_bytes(size, seed=5)
    finally:
        await cli.close()
