"""
An end-to-end encrypted view of a peer connection (D60).

Wraps anything with ``send``/``drain``/``recv``/``close`` (WebRTCPeer) and
offers the same interface, so the session and transfer layers don't
change. ``hello``, ``key_exchange`` and ``bye`` are the only messages
sent in the clear (still inside DTLS); anything else sent before the
exchange completes is held back, then sealed. Once established, a
plaintext message other than ``bye`` is a protocol violation and closes
the connection.

``bye`` stays in the clear so it can be sent synchronously at the last
moment (a closing browser tab can't await encryption). It carries
nothing, and a forged one can only end the session — which anyone able
to forge it could do anyway by dropping packets.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

from packages.crypto.e2e import (
    KIND_ENCRYPTED,
    E2EError,
    Established,
    Identity,
    KeyExchange,
    KeyExchangeMessage,
)

log = logging.getLogger(__name__)

#: Bytes an encrypted frame adds: kind + counter + GCM tag + payload type.
OVERHEAD = 1 + 8 + 16 + 1


class _Peer(Protocol):
    @property
    def closed(self) -> bool: ...

    @property
    def max_message_size(self) -> int: ...

    def send(self, data: str | bytes) -> None: ...

    async def drain(self) -> None: ...

    async def recv(self) -> str | bytes | None: ...

    async def close(self) -> None: ...


class SecureChannel:
    def __init__(self, peer: _Peer, identity: Identity) -> None:
        self._peer = peer
        self._exchange = KeyExchange(identity)
        self.established: Established | None = None
        #: Why the channel was given up, if it was.
        self.failure: str | None = None
        self._held: list[str | bytes] = []
        self._settled = asyncio.Event()  # established or failed

    # ── Connection-like interface ───────────────────────────────────

    @property
    def closed(self) -> bool:
        return self._peer.closed or self.failure is not None

    @property
    def max_message_size(self) -> int:
        return self._peer.max_message_size - OVERHEAD

    def send(self, data: str | bytes) -> None:
        """Send end-to-end encrypted, or hold until the exchange completes."""
        if self.failure is not None:
            raise ConnectionError(self.failure)
        if self.established is None:
            self._held.append(data)
            return
        self._peer.send(self.established.sealer.seal(data))

    def send_plain(self, data: str) -> None:
        """For hello, key_exchange and bye only."""
        self._peer.send(data)

    async def drain(self) -> None:
        await self._settled.wait()
        if self.failure is not None:
            raise ConnectionError(self.failure)
        await self._peer.drain()

    async def recv(self) -> str | bytes | None:
        """The next message, decrypted once established; None when closed."""
        data = await self._peer.recv()
        if data is None or self.failure is not None:
            return None
        if self.established is None:
            return data  # hello / key_exchange, handled by the session
        if isinstance(data, bytes) and data[:1] == bytes([KIND_ENCRYPTED]):
            try:
                return self.established.sealer.open(data)
            except E2EError as exc:
                await self.fail(str(exc))
                return None
        if isinstance(data, str) and _is_bye(data):
            return data
        await self.fail("the peer sent an unencrypted message after the key exchange")
        return None

    async def close(self) -> None:
        await self._peer.close()

    # ── Exchange ────────────────────────────────────────────────────

    def key_exchange_message(self) -> KeyExchangeMessage:
        return self._exchange.message()

    def complete(self, peer_message: KeyExchangeMessage) -> Established:
        """Finish the exchange and release held messages. Raises E2EError."""
        if self.established is not None:
            raise E2EError("a second key exchange on the same connection")
        self.established = self._exchange.complete(peer_message)
        held, self._held = self._held, []
        for data in held:
            self._peer.send(self.established.sealer.seal(data))
        self._settled.set()
        return self.established

    async def fail(self, reason: str) -> None:
        """Give up on this connection: it can't be trusted."""
        if self.failure is None:
            log.warning("end-to-end encryption failed: %s", reason)
            self.failure = reason
            self._held.clear()
            self._settled.set()
            await self._peer.close()


def _is_bye(text: str) -> bool:
    try:
        return bool(json.loads(text).get("type") == "bye")
    except (ValueError, AttributeError):
        return False
