"""
Application protocol over an open DataChannel: hello, chat, ping and
file transfer. Terminal output goes through ``Ui``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from packages.protocol.codec import decode, encode
from packages.protocol.enums import Capability, MessageType
from packages.protocol.exceptions import ProtocolError
from packages.protocol.models import (
    ByePayload,
    ChatPayload,
    ErrorPayload,
    HelloPayload,
    Payload,
    PingPayload,
    PongPayload,
    ProtocolMessage,
    make_message,
)
from packages.rtc.peer import WebRTCPeer
from packages.transfer.frames import FrameDecodeError
from packages.transfer.manager import Direction, Transfer, TransferManager, TransferStatus

log = logging.getLogger(__name__)

CAPABILITIES = [
    Capability.CHAT,
    Capability.FILE_TRANSFER,
    Capability.CANCEL,
    Capability.RESUME,
    Capability.ACK,
]


def shown_bytes(t: Transfer) -> int:
    """Progress to display: for a sender with acks, what the receiver has
    stored rather than what was handed to the network."""
    return t.bytes_acked if t.direction is Direction.SEND and t.acked else t.bytes_done


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError("unreachable")


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{math.ceil(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m {math.ceil(seconds % 60)}s"
    return f"{minutes // 60}h {minutes % 60}m"


class Style:
    """ANSI styling, or plain text when output isn't a colour terminal
    (piped, or NO_COLOR set: https://no-color.org)."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled else text

    def ok(self, text: str) -> str:
        return self._wrap("32", text)

    def error(self, text: str) -> str:
        return self._wrap("31", text)

    def accent(self, text: str) -> str:
        return self._wrap("36", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)


def progress_bar(fraction: float, width: int) -> str:
    """``████████░░░░`` filled to ``fraction`` (0..1)."""
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return "█" * filled + "░" * (width - filled)


class Ui:
    """Terminal output. On a TTY, progress is a single redrawn line with a
    bar, speed and ETA; otherwise only start and finish are printed."""

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self._stream = stream
        self._tty = stream.isatty()
        self.style = Style(self._tty and not os.environ.get("NO_COLOR"))
        self._progress_shown = False
        self._started: dict[str, float] = {}

    def line(self, text: str) -> None:
        self._clear_progress()
        print(text, file=self._stream, flush=True)

    def ok(self, text: str) -> None:
        self.line(self.style.ok(f"✓ {text}"))

    def error(self, text: str) -> None:
        self.line(self.style.error(f"✗ {text}"))

    def hint(self, text: str) -> None:
        self.line(self.style.dim(text))

    def transfer_update(self, t: Transfer) -> None:
        arrow = self.style.accent("↑" if t.direction is Direction.SEND else "↓")
        started = self._started.setdefault(t.id, time.monotonic())
        elapsed = max(time.monotonic() - started, 1e-6)
        sent_now = shown_bytes(t) - t.resumed_from  # speed counts this session only
        speed = sent_now / elapsed

        if t.status is TransferStatus.STREAMING:
            if t.bytes_done == t.resumed_from:  # first report for this transfer
                note = ""
                if t.resumed_from:
                    pct = 100 * t.resumed_from / t.size
                    note = f", resuming from {pct:.0f}%"
                if not self._tty or note:
                    self.line(f"{arrow} {t.filename} ({format_bytes(t.size)}{note})...")
            if not self._tty:
                return
            self._draw_progress(arrow, t, speed)
            return

        self._started.pop(t.id, None)
        if t.status is TransferStatus.COMPLETED:
            where = f"saved to {t.path}" if t.direction is Direction.RECEIVE else "delivered"
            details = format_bytes(t.size)
            if sent_now >= 1024 * 1024:  # below this, setup time dominates the "speed"
                details += f", {format_bytes(sent_now / elapsed)}/s"
            if t.resumed_from:
                details += f", resumed from {100 * t.resumed_from / t.size:.0f}%"
            self.line(f"{arrow} {t.filename}: {self.style.ok(where)} {self.style.dim(details)}")
        else:
            self.error(f"{t.filename}: {t.status} ({t.reason})")

    def _draw_progress(self, arrow: str, t: Transfer, speed: float) -> None:
        done = shown_bytes(t)
        fraction = done / t.size if t.size else 1.0
        eta = format_duration((t.size - done) / speed) if speed > 0 else "?"
        stats = (
            f" {100 * fraction:3.0f}%  {format_bytes(done)}/{format_bytes(t.size)}"
            f"  {format_bytes(speed)}/s  ETA {eta}"
        )
        columns = shutil.get_terminal_size().columns
        # arrow + space + name + space + bar + stats, fitted to the terminal
        name_width = max(8, min(len(t.filename), columns // 4))
        name = t.filename if len(t.filename) <= name_width else t.filename[: name_width - 1] + "…"
        bar_width = max(10, columns - len(stats) - name_width - 5)
        bar = self.style.accent(progress_bar(fraction, bar_width))
        self._stream.write(f"\r\x1b[K{arrow} {name:<{name_width}} {bar}{self.style.dim(stats)}")
        self._stream.flush()
        self._progress_shown = True

    def _clear_progress(self) -> None:
        if self._progress_shown:
            self._stream.write("\r\x1b[K")
            self._progress_shown = False


class Session:
    """The protocol conversation with the other peer. Survives reconnects:
    ``attach`` moves it onto each new connection (D50)."""

    def __init__(
        self,
        peer: WebRTCPeer,
        download_dir: Path,
        ui: Ui,
        *,
        on_stall: Callable[[], None] | None = None,
    ) -> None:
        self._peer = peer
        self._ui = ui
        self.transfers = TransferManager(
            peer, download_dir, on_update=ui.transfer_update, on_stall=on_stall
        )
        #: Resolved with the peer's capabilities when its hello arrives.
        self.peer_hello: asyncio.Future[set[Capability]] = (
            asyncio.get_running_loop().create_future()
        )
        #: Set while attached to a live connection.
        self.connected = asyncio.Event()
        self.connected.set()
        #: The other side said bye: it left on purpose, don't reconnect.
        self.peer_left = False
        #: Bumped on every reconnect, so waiters can tell a new connection
        #: from the one that just failed.
        self.generation = 0

    def start(self) -> None:
        self._send(MessageType.HELLO, HelloPayload(capabilities=CAPABILITIES))

    def attach(self, peer: WebRTCPeer) -> None:
        """Continue on a new connection after a reconnect."""
        self._peer = peer
        self.transfers.attach(peer)
        self.peer_hello = asyncio.get_running_loop().create_future()
        self.generation += 1
        self.connected.set()
        self.start()

    async def wait_ready(self, *, after: int, timeout: float) -> bool:
        """Wait for a connection newer than generation ``after`` whose hello
        has arrived. ``after=-1`` accepts the current one."""

        async def ready() -> None:
            while self.generation <= after or not self.connected.is_set():
                await asyncio.sleep(0.05)
            await asyncio.shield(self.peer_hello)

        try:
            await asyncio.wait_for(ready(), timeout)
        except TimeoutError:
            return False
        return True

    def say_bye(self) -> None:
        """Tell the other side we're leaving on purpose."""
        with contextlib.suppress(ConnectionError):
            self._send(MessageType.BYE, ByePayload())

    async def run_inbound(self) -> None:
        """Process incoming messages until the channel closes."""
        while (data := await self._peer.recv()) is not None:
            if isinstance(data, bytes):
                try:
                    self.transfers.handle_frame(data)
                except FrameDecodeError as exc:
                    self._send_error("bad_frame", str(exc))
                continue

            try:
                msg = decode(data.encode())
            except ProtocolError as exc:
                self._send_error("bad_message", str(exc))
                continue
            log.debug("recv %s", msg.type)
            await self._dispatch(msg)

        self.connected.clear()
        self.transfers.close("connection closed")

    async def _dispatch(self, msg: ProtocolMessage) -> None:
        payload = msg.payload
        if await self.transfers.handle_message(msg):
            return
        if isinstance(payload, HelloPayload):
            if not self.peer_hello.done():
                self.peer_hello.set_result(set(payload.capabilities))
        elif isinstance(payload, ChatPayload):
            self._ui.line(f"{self._ui.style.bold('peer:')} {payload.text}")
        elif isinstance(payload, PingPayload):
            self._send(MessageType.PONG, PongPayload(timestamp=payload.timestamp))
        elif isinstance(payload, PongPayload):
            rtt_ms = (time.monotonic() - payload.timestamp) * 1000
            self._ui.hint(f"pong: {rtt_ms:.1f} ms round trip")
        elif isinstance(payload, ByePayload):
            self.peer_left = True
        elif isinstance(payload, ErrorPayload):
            # Never answer an error with an error — that's how loops start
            self._ui.error(f"peer reported an error: {payload.code}: {payload.message}")
        else:
            log.debug("ignoring %s message", msg.type)

    def send_chat(self, text: str) -> None:
        self._send(MessageType.CHAT, ChatPayload(text=text))

    def ping(self) -> None:
        # Monotonic is fine across machines: the peer only echoes it back
        self._send(MessageType.PING, PingPayload(timestamp=time.monotonic()))

    def send_options(self) -> dict[str, bool]:
        """send_file options for what the peer supports."""
        return {
            "resume": self.peer_can(Capability.RESUME),
            "ack": self.peer_can(Capability.ACK),
        }

    def peer_can(self, capability: Capability) -> bool:
        return self.peer_hello.done() and capability in self.peer_hello.result()

    def _send(self, msg_type: MessageType, payload: Payload) -> None:
        log.debug("send %s", msg_type)
        self._peer.send(encode(make_message(msg_type, payload)).decode())

    def _send_error(self, code: str, message: str) -> None:
        log.warning("bad input from peer (%s): %s", code, message)
        self._send(MessageType.ERROR, ErrorPayload(code=code, message=message))
