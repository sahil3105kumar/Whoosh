"""
Application protocol over an open DataChannel: hello, chat, ping and
file transfer. Terminal output goes through ``Ui``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import TextIO

from packages.protocol.codec import decode, encode
from packages.protocol.enums import Capability, MessageType
from packages.protocol.exceptions import ProtocolError
from packages.protocol.models import (
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

CAPABILITIES = [Capability.CHAT, Capability.FILE_TRANSFER, Capability.CANCEL]


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError("unreachable")


class Ui:
    """Terminal output. On a TTY, progress is a single line redrawn in
    place; otherwise only start and finish are printed."""

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self._stream = stream
        self._tty = stream.isatty()
        self._progress_shown = False
        self._started: dict[str, float] = {}

    def line(self, text: str) -> None:
        self._clear_progress()
        print(text, file=self._stream, flush=True)

    def transfer_update(self, t: Transfer) -> None:
        arrow = "↑" if t.direction is Direction.SEND else "↓"
        started = self._started.setdefault(t.id, time.monotonic())
        elapsed = max(time.monotonic() - started, 1e-6)

        if t.status is TransferStatus.STREAMING:
            if not self._tty:
                if t.bytes_done == 0:
                    self.line(f"{arrow} {t.filename} ({format_bytes(t.size)})...")
                return
            pct = 100 * t.bytes_done / t.size if t.size else 100.0
            self._stream.write(
                f"\r\x1b[K{arrow} {t.filename}  {pct:5.1f}%  "
                f"{format_bytes(t.bytes_done)} / {format_bytes(t.size)}  "
                f"{format_bytes(t.bytes_done / elapsed)}/s"
            )
            self._stream.flush()
            self._progress_shown = True
            return

        self._started.pop(t.id, None)
        if t.status is TransferStatus.COMPLETED:
            where = f"saved to {t.path}" if t.direction is Direction.RECEIVE else "delivered"
            details = format_bytes(t.size)
            if t.size >= 1024 * 1024:  # below this, setup time dominates the "speed"
                details += f", {format_bytes(t.size / elapsed)}/s"
            self.line(f"{arrow} {t.filename}: {where} ({details})")
        else:
            self.line(f"✗ {t.filename}: {t.status} ({t.reason})")

    def _clear_progress(self) -> None:
        if self._progress_shown:
            self._stream.write("\r\x1b[K")
            self._progress_shown = False


class Session:
    def __init__(self, peer: WebRTCPeer, download_dir: Path, ui: Ui) -> None:
        self._peer = peer
        self._ui = ui
        self.transfers = TransferManager(peer, download_dir, on_update=ui.transfer_update)
        #: Resolved with the peer's capabilities when its hello arrives.
        self.peer_hello: asyncio.Future[set[Capability]] = (
            asyncio.get_running_loop().create_future()
        )

    def start(self) -> None:
        self._send(MessageType.HELLO, HelloPayload(capabilities=CAPABILITIES))

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
            await self._dispatch(msg)

        self.transfers.close("connection closed")

    async def _dispatch(self, msg: ProtocolMessage) -> None:
        payload = msg.payload
        if await self.transfers.handle_message(msg):
            return
        if isinstance(payload, HelloPayload):
            if not self.peer_hello.done():
                self.peer_hello.set_result(set(payload.capabilities))
        elif isinstance(payload, ChatPayload):
            self._ui.line(f"peer: {payload.text}")
        elif isinstance(payload, PingPayload):
            self._send(MessageType.PONG, PongPayload(timestamp=payload.timestamp))
        elif isinstance(payload, PongPayload):
            rtt_ms = (time.monotonic() - payload.timestamp) * 1000
            self._ui.line(f"pong: {rtt_ms:.1f} ms round trip")
        elif isinstance(payload, ErrorPayload):
            # Never answer an error with an error — that's how loops start
            self._ui.line(f"peer reported an error: {payload.code}: {payload.message}")
        else:
            log.debug("ignoring %s message", msg.type)

    def send_chat(self, text: str) -> None:
        self._send(MessageType.CHAT, ChatPayload(text=text))

    def ping(self) -> None:
        # Monotonic is fine across machines: the peer only echoes it back
        self._send(MessageType.PING, PingPayload(timestamp=time.monotonic()))

    def peer_can(self, capability: Capability) -> bool:
        return self.peer_hello.done() and capability in self.peer_hello.result()

    def _send(self, msg_type: MessageType, payload: Payload) -> None:
        self._peer.send(encode(make_message(msg_type, payload)).decode())

    def _send_error(self, code: str, message: str) -> None:
        log.warning("bad input from peer (%s): %s", code, message)
        self._send(MessageType.ERROR, ErrorPayload(code=code, message=message))
