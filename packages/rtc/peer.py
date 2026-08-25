"""
Minimal WebRTC peer wrapper around aiortc.

Handles SDP offer/answer exchange and DataChannel lifecycle.
The caller wires signaling transport (WebSocket, etc.) via callbacks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiortc import RTCConfiguration, RTCDataChannel, RTCPeerConnection, RTCSessionDescription

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ponytail: empty iceServers skips STUN. Fine for same-network / local.
# For NAT traversal, pass a config with real STUN/TURN servers.
_DEFAULT_CONFIG = RTCConfiguration(iceServers=[])


class WebRTCPeer:
    """Wraps an RTCPeerConnection to establish a single DataChannel."""

    def __init__(
        self,
        on_signaling_message: Callable[[dict], Awaitable[None]],
        on_message: Callable[[str | bytes], Awaitable[None]] | None = None,
        config: RTCConfiguration | None = None,
    ) -> None:
        self.pc = RTCPeerConnection(configuration=config or _DEFAULT_CONFIG)
        self.channel: RTCDataChannel | None = None
        self._on_sig = on_signaling_message
        self._on_msg = on_message
        self.channel_ready: asyncio.Future[bool] = asyncio.get_event_loop().create_future()

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

    async def handle_signaling(self, msg: dict) -> None:
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

    def _wire_channel(self) -> None:
        if self.channel is None:
            return

        # Channel may already be open (race between event and handler setup)
        if self.channel.readyState == "open" and not self.channel_ready.done():
            self.channel_ready.set_result(True)

        @self.channel.on("open")
        def on_open() -> None:
            if not self.channel_ready.done():
                self.channel_ready.set_result(True)

        @self.channel.on("message")
        def on_message(data: str | bytes) -> None:
            if self._on_msg:
                asyncio.ensure_future(self._on_msg(data))

    async def close(self) -> None:
        await self.pc.close()
