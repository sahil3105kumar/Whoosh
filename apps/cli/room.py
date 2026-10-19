"""
Everyone in the room, from the CLI's point of view (#23–#27).

``Room`` turns Mesh events into one ``Session`` per peer — kept across
reconnects, so transfers resume — and offers room-wide operations:
broadcast (chat, ping) and sending a file to everyone at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from apps.cli.connection import (
    RECONNECT_TIMEOUT,
    ConnectError,
    Fatal,
    Mesh,
    PeerConnected,
    PeerGone,
    PeerUnreachable,
)
from apps.cli.identity import TrustStore, load_identity
from apps.cli.session import Session, Ui
from packages.transfer.manager import Transfer

#: Attempts per file per peer when the connection keeps dropping (#19).
MAX_SEND_ATTEMPTS = 3


class Room:
    def __init__(self, mesh: Mesh, ui: Ui, download_dir: Path, name: str | None) -> None:
        self.mesh = mesh
        self._ui = ui
        self._download_dir = download_dir
        self._name = name
        #: This device's identity and verified devices (#30), loaded once.
        self._identity = load_identity()
        self._trust = TrustStore()
        self.sessions: dict[str, Session] = {}
        #: Sessions of peers who have left, kept for their transfer results.
        self._former: list[Session] = []
        #: When each disconnected peer dropped, to give up after a while.
        self._lost_since: dict[str, float] = {}
        self._unreachable_reported: set[str] = set()
        self._tasks: set[asyncio.Task[object]] = set()
        self._changed = asyncio.Event()
        #: The room became unusable before anyone connected.
        self.error: ConnectError | None = None
        #: Someone has been connected at some point.
        self.ever_connected = False

    # ── Membership ──────────────────────────────────────────────────

    def connected(self) -> list[Session]:
        """Peers we can talk to right now (connected and introduced)."""
        return [s for s in self.sessions.values() if s.connected.is_set() and s.peer_hello.done()]

    def everyone(self) -> list[Session]:
        """Every session this room has had, including peers who left."""
        return [*self._former, *self.sessions.values()]

    async def wait_for_peers(self, count: int = 1) -> None:
        """Wait until ``count`` peers are connected. Raises ConnectError if
        the room becomes unusable first."""
        while len(self.connected()) < count:
            if self.error is not None:
                raise self.error
            await self._wait_changed(timeout=1.0)

    async def wait_until_empty(self) -> None:
        """After someone has been here: wait until everyone has left."""
        while not (self.ever_connected and not self.sessions):
            if self.error is not None and not self.ever_connected:
                raise self.error
            await self._wait_changed(timeout=1.0)

    # ── Room-wide operations ────────────────────────────────────────

    def broadcast(self, send: Callable[[Session], None]) -> int:
        """Run ``send`` on every connected peer's session (#25): the
        primitive behind group chat and room-wide pings. Returns how many
        peers it reached."""
        reached = 0
        for session in self.connected():
            with contextlib.suppress(ConnectionError):
                send(session)
                reached += 1
        return reached

    async def send_to_all(self, path: Path) -> list[Transfer]:
        """Send ``path`` to everyone connected, in parallel (#27). Each peer
        gets its own transfer, with its own resume and retries."""
        targets = self.connected()
        results = await asyncio.gather(*(self.send_with_retry(s, path) for s in targets))
        return [t for t in results if t is not None]

    async def send_with_retry(self, session: Session, path: Path) -> Transfer | None:
        """Send ``path`` to one peer; if the connection drops, wait for the
        reconnect and send again. Resume means a retry only sends what's
        missing."""
        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            generation = session.generation
            try:
                transfer = await session.transfers.send_file(path, **session.send_options())
            except OSError as exc:
                self._ui.error(f"{path}: {exc}")
                return None
            if not transfer.interrupted or attempt == MAX_SEND_ATTEMPTS:
                return transfer
            self._ui.hint(
                f"{path.name} will continue when {session.peer_name} is back "
                f"(retry {attempt} of {MAX_SEND_ATTEMPTS - 1})."
            )
            if not await session.wait_ready(after=generation, timeout=RECONNECT_TIMEOUT + 30):
                return transfer
        return None

    def find(self, name: str) -> Session | None:
        """A connected peer by display name."""
        matches = [s for s in self.sessions.values() if s.peer_name == name]
        return matches[0] if len(matches) == 1 else None

    def verify(self, session: Session, *, matches: bool) -> None:
        """The user compared codes with ``session``'s peer (#32)."""
        if matches:
            session.mark_verified()
            self._ui.ok(f"{session.peer_name} is now a verified device.")
            return
        self._ui.error(
            f"The codes didn't match: someone may be intercepting your connection to "
            f"{session.peer_name}. Disconnecting."
        )
        for peer_id, s in list(self.sessions.items()):
            if s is session:
                session.refused = True
                self.mesh.forget(peer_id)
                self._remove(peer_id, "was disconnected.")

    def say_bye(self) -> None:
        self.broadcast(lambda s: s.say_bye())

    # ── Event loop ──────────────────────────────────────────────────

    async def run(self) -> None:
        """Process mesh events for as long as the room is in use."""
        while True:
            try:
                event = await asyncio.wait_for(self.mesh.events.get(), timeout=1.0)
            except TimeoutError:
                self._expire_lost()
                continue
            if isinstance(event, PeerConnected):
                self._on_connected(event.peer_id, event)
            elif isinstance(event, PeerGone):
                self._remove(event.peer_id, "left the room.")
            elif isinstance(event, PeerUnreachable):
                if event.peer_id not in self._unreachable_reported:
                    self._unreachable_reported.add(event.peer_id)
                    self._ui.error(f"Can't reach peer-{event.peer_id[:4]}: {event.message}")
            elif isinstance(event, Fatal):
                if not self.ever_connected:
                    self.error = event.error
                else:
                    # Existing connections carry on; nobody new can join
                    self._ui.error(f"{event.error} Existing connections are unaffected.")
            self._pulse()

    def _on_connected(self, peer_id: str, event: PeerConnected) -> None:
        self.ever_connected = True
        self._lost_since.pop(peer_id, None)
        self._unreachable_reported.discard(peer_id)
        session = self.sessions.get(peer_id)
        if session is not None:
            session.attach(event.peer)
            self._ui.ok(f"Reconnected to {session.peer_name}.")
        else:
            session = Session(
                event.peer,
                self._download_dir,
                self._ui,
                on_stall=lambda: self._spawn(self.mesh.drop(peer_id)),
                name=self._name,
                peer_id=peer_id,
                identity=self._identity,
                trust=self._trust,
            )
            self.sessions[peer_id] = session
            session.start()
            self._spawn(self._announce(session))
        self._spawn(self._run_peer(peer_id, session))

    async def _announce(self, session: Session) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await session.peer_hello
            self._ui.ok(f"{session.peer_name} joined.")
            self._pulse()

    async def _run_peer(self, peer_id: str, session: Session) -> None:
        """One connection's lifetime: its messages, then what its end means."""
        generation = session.generation
        await session.run_inbound()
        if session.generation != generation:
            return  # already replaced by a reconnect
        if session.peer_left:
            self.mesh.forget(peer_id)
            self._remove(peer_id, "left.")
        else:
            self._lost_since[peer_id] = time.monotonic()
            self._ui.hint(f"Connection to {session.peer_name} lost. Reconnecting...")
        self._pulse()

    def _expire_lost(self) -> None:
        now = time.monotonic()
        for peer_id, since in list(self._lost_since.items()):
            if now - since > RECONNECT_TIMEOUT:
                self.mesh.forget(peer_id)
                self._remove(peer_id, "didn't come back.")
                self._pulse()

    def _remove(self, peer_id: str, why: str) -> None:
        self._lost_since.pop(peer_id, None)
        session = self.sessions.pop(peer_id, None)
        if session is not None:
            self._former.append(session)
            session.transfers.close("peer left")
            self._ui.line(f"{session.peer_name} {why}")

    # ── Plumbing ────────────────────────────────────────────────────

    def _pulse(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    async def _wait_changed(self, timeout: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._changed.wait(), timeout)

    def _spawn(self, coro: Coroutine[Any, Any, object]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for session in self.sessions.values():
            for transfer in session.transfers.transfers:
                session.transfers.cancel(transfer.id, "session ended")
        await self.mesh.close()
