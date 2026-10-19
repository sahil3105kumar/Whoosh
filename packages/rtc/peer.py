"""
Minimal WebRTC peer wrapper around aiortc.

Handles SDP offer/answer exchange and DataChannel lifecycle.
The caller wires signaling transport (WebSocket, etc.) via a callback
and reads incoming DataChannel messages, in order, with ``recv()``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiortc import RTCConfiguration, RTCDataChannel, RTCPeerConnection, RTCSessionDescription

logger = logging.getLogger(__name__)

# ponytail: empty iceServers skips STUN. Fine for same-network / local.
# For NAT traversal, pass a config with real STUN/TURN servers.
_DEFAULT_CONFIG = RTCConfiguration(iceServers=[])

#: drain() blocks while more than this many bytes are queued for sending...
HIGH_WATER_BYTES = 1024 * 1024
#: ...and resumes once the queue falls to this level.
LOW_WATER_BYTES = 256 * 1024


class WebRTCPeer:
    """Wraps an RTCPeerConnection to establish a single DataChannel."""

    def __init__(
        self,
        on_signaling_message: Callable[[dict[str, Any]], Awaitable[None]],
        config: RTCConfiguration | None = None,
    ) -> None:
        self.pc = RTCPeerConnection(configuration=config or _DEFAULT_CONFIG)
        self.channel: RTCDataChannel | None = None
        self._on_sig = on_signaling_message
        loop = asyncio.get_running_loop()
        self.channel_ready: asyncio.Future[bool] = loop.create_future()
        # Incoming messages in arrival order; None marks channel close.
        self._inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self._buffer_low = asyncio.Event()

        @self.pc.on("datachannel")
        def on_datachannel(ch: RTCDataChannel) -> None:
            self.channel = ch
            self._wire_channel()

    async def create_offer(self) -> None:
        """Initiator calls this to create the DataChannel and send an offer."""
        self.channel = self.pc.createDataChannel("whoosh")
        self._wire_channel()

        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)

        assert self.pc.localDescription is not None
        await self._on_sig(
            {"type": self.pc.localDescription.type, "sdp": self.pc.localDescription.sdp}
        )

    async def handle_signaling(self, msg: dict[str, Any]) -> None:
        """Process an incoming SDP offer or answer from the remote peer."""
        msg_type = msg.get("type")
        if msg_type not in ("offer", "answer"):
            return

        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=msg["sdp"], type=msg_type))

        if msg_type == "offer":
            answer = await self.pc.createAnswer()
            await self.pc.setLocalDescription(answer)
            assert self.pc.localDescription is not None
            await self._on_sig(
                {"type": self.pc.localDescription.type, "sdp": self.pc.localDescription.sdp}
            )

    async def recv(self) -> str | bytes | None:
        """Next DataChannel message (str for text frames, bytes for binary),
        or None once the channel has closed.

        A queue rather than a callback so the application processes
        messages strictly in order, and its exceptions land in its own
        task instead of in a fire-and-forget future.
        """
        return await self._inbox.get()

    def send(self, data: str | bytes) -> None:
        """Send one message. Raises ConnectionError if the channel isn't open."""
        if self.channel is None or self.channel.readyState != "open":
            raise ConnectionError("DataChannel is not open")
        self.channel.send(data)

    async def drain(self) -> None:
        """Wait until the send buffer is below HIGH_WATER_BYTES.

        Bulk senders call this before each send so a large file streams
        at network speed instead of being queued into memory whole.
        """
        while True:
            if self.channel is None or self.channel.readyState != "open":
                raise ConnectionError("DataChannel is not open")
            if self.channel.bufferedAmount <= HIGH_WATER_BYTES:
                return
            # No await between the check and clear(), so the low-water
            # event can't fire in between and be lost.
            self._buffer_low.clear()
            await self._buffer_low.wait()

    def _wire_channel(self) -> None:
        channel = self.channel
        if channel is None:
            return

        channel.bufferedAmountLowThreshold = LOW_WATER_BYTES

        # Channel may already be open (race between event and handler setup)
        if channel.readyState == "open" and not self.channel_ready.done():
            self.channel_ready.set_result(True)

        @channel.on("open")
        def on_open() -> None:
            if not self.channel_ready.done():
                self.channel_ready.set_result(True)

        @channel.on("message")
        def on_message(data: str | bytes) -> None:
            self._inbox.put_nowait(data)

        @channel.on("bufferedamountlow")
        def on_buffered_amount_low() -> None:
            self._buffer_low.set()

        @channel.on("close")
        def on_close() -> None:
            self._inbox.put_nowait(None)
            self._buffer_low.set()  # wake drain() so it sees the closed state

    async def close(self) -> None:
        await self.pc.close()
