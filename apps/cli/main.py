"""
Whoosh CLI — chat and send files peer-to-peer.

Usage:
    whoosh create [--out DIR]          create a room, then chat (/send to send files)
    whoosh join ROOM [--out DIR]       join a room, then chat
    whoosh send FILE [FILE ...]        create a room, send files, exit
    whoosh receive ROOM [--out DIR]    join a room, save incoming files, exit

    ROOM is a room code (a1b2c3d4) or an invite link (https://host/#a1b2c3d4);
    a link also selects its server.

    Global: --server URL (default $WHOOSH_SERVER, else http://localhost:8000),
            -v for debug logs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import re
import shlex
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from websockets.exceptions import WebSocketException

from apps.cli.connection import RECONNECT_TIMEOUT, ConnectError, Link, create_room
from apps.cli.queue import SendQueue
from apps.cli.session import Session, Ui, format_bytes
from packages.protocol.enums import Capability
from packages.transfer.manager import Transfer, TransferStatus

log = logging.getLogger(__name__)

DEFAULT_SERVER = "http://localhost:8000"

HELP = """\
Type a message and press Enter to chat. Commands:
  /send PATH [PATH ...]   queue files to send, one at a time (quote paths with spaces)
  /queue                  show the file being sent and those waiting
  /cancel                 cancel the current transfer and clear the queue
  /ping                   measure round-trip time
  /quit                   leave"""

#: How long to wait for the peer's hello before sending files.
_HELLO_TIMEOUT = 10.0

_ROOM_CODE = re.compile(r"^[0-9a-f]{8}$")


# ── Arguments ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class RoomRef:
    code: str
    #: Server taken from an invite link, if one was given.
    server: str | None = None


def room_ref(value: str) -> RoomRef:
    """argparse type: a room code, or an invite link carrying one."""
    value = value.strip()
    if _ROOM_CODE.match(value.lower()):
        return RoomRef(value.lower())
    url = urllib.parse.urlsplit(value)
    if url.scheme in ("http", "https") and url.netloc and _ROOM_CODE.match(url.fragment.lower()):
        return RoomRef(url.fragment.lower(), f"{url.scheme}://{url.netloc}")
    raise argparse.ArgumentTypeError(
        f"{value!r} isn't a room code (8 characters like a1b2c3d4) or an invite link"
    )


# ── stdin ───────────────────────────────────────────────────────────


def _stdin_lines() -> asyncio.Queue[str | None]:
    """Lines from stdin, fed by a daemon thread (portable, unlike
    connect_read_pipe, and won't block interpreter exit). None = EOF."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def reader() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line.rstrip("\n"))
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=reader, daemon=True).start()
    return queue


# ── Modes ───────────────────────────────────────────────────────────


async def _stay_connected(link: Link, session: Session, ui: Ui) -> str:
    """Run the session, reconnecting whenever the connection drops (D50).
    Returns why it ended: "left" (the other side said bye) or "lost"."""
    while True:
        await session.run_inbound()  # until this connection ends
        if session.peer_left:
            return "left"
        ui.hint("Connection lost. Reconnecting...")
        try:
            peer = await link.reconnect()
        except ConnectError as exc:
            ui.error(str(exc))
            return "lost"
        session.attach(peer)
        ui.ok("Reconnected.")


async def _interactive(link: Link, session: Session, ui: Ui) -> None:
    ui.ok("Connected peer-to-peer.")
    ui.hint("Type a message and press Enter, /send PATH to send files, /help for more.")
    queue = SendQueue(lambda path: _send_with_retry(session, path, ui))
    connection = asyncio.create_task(_stay_connected(link, session, ui))
    commands = asyncio.create_task(_command_loop(session, queue, ui))

    try:
        done, _ = await asyncio.wait({connection, commands}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # re-raise failures
        if connection in done and connection.result() == "left":
            ui.line("The other person left.")
    finally:
        session.say_bye()
        dropped = queue.clear()
        if dropped:
            ui.hint(f"{len(dropped)} queued file(s) weren't sent.")
        for transfer in session.transfers.transfers:
            session.transfers.cancel(transfer.id, "session ended")
        await queue.close()
        for task in (connection, commands):
            task.cancel()


async def _command_loop(session: Session, queue: SendQueue, ui: Ui) -> None:
    lines = _stdin_lines()

    while (line := await lines.get()) is not None:
        line = line.strip()
        if not line:
            continue
        try:
            if not _command(line, session, queue, ui):
                return
        except ConnectionError:
            ui.hint("Not connected right now (reconnecting); that wasn't sent.")


def _command(line: str, session: Session, queue: SendQueue, ui: Ui) -> bool:
    """Handle one line of input. False means quit."""
    if not line.startswith("/"):
        session.send_chat(line)
        return True

    cmd, _, arg = line.partition(" ")
    if cmd == "/quit":
        return False
    elif cmd == "/help":
        ui.line(HELP)
    elif cmd == "/ping":
        session.ping()
    elif cmd == "/queue":
        _show_queue(queue, ui)
    elif cmd == "/cancel":
        dropped = queue.clear()
        cancelled = [t for t in session.transfers.transfers if session.transfers.cancel(t.id)]
        if not cancelled and not dropped:
            ui.hint("Nothing to cancel.")
        elif dropped:
            ui.hint(f"Removed {len(dropped)} queued file(s).")
    elif cmd == "/send":
        if not session.peer_can(Capability.FILE_TRANSFER):
            ui.error("The other side doesn't support file transfer.")
            return True
        paths = _parse_paths(arg, ui)
        if paths:
            busy = queue.current is not None
            queue.add(paths)
            if busy or len(paths) > 1:
                ui.hint(f"Queued {len(paths)} file(s); /queue to see the list.")
    else:
        ui.error(f"Unknown command {cmd}. Type /help for the list.")
    return True


def _show_queue(queue: SendQueue, ui: Ui) -> None:
    if queue.current is None and not queue.waiting:
        ui.hint("Queue is empty.")
        return
    if queue.current is not None:
        ui.line(f"  sending  {queue.current.name}")
    for i, path in enumerate(queue.waiting, 1):
        size = format_bytes(path.stat().st_size) if path.exists() else "missing"
        ui.line(f"  #{i:<6} {path.name} {ui.style.dim(f'({size})')}")


def _parse_paths(arg: str, ui: Ui) -> list[Path]:
    try:
        raw = shlex.split(arg)
    except ValueError as exc:
        ui.error(f"Can't parse paths: {exc}")
        return []
    if not raw:
        ui.hint("Usage: /send PATH [PATH ...]")
    paths = []
    for item in raw:
        path = Path(item).expanduser()
        if path.is_file():
            paths.append(path)
        elif path.is_dir():
            ui.error(f"{path} is a folder; send the files inside it, or zip it first.")
        else:
            ui.error(f"No such file: {path}")
    return paths


#: Attempts per file when the connection keeps dropping (#19).
MAX_SEND_ATTEMPTS = 3


async def _send_with_retry(session: Session, path: Path, ui: Ui) -> Transfer | None:
    """Send ``path``; if the connection drops, wait for the reconnect and
    send it again. Resume means the retry only sends what's missing."""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        generation = session.generation
        try:
            transfer = await session.transfers.send_file(path, **session.send_options())
        except OSError as exc:
            ui.error(f"{path}: {exc}")
            return None
        if not transfer.interrupted or attempt == MAX_SEND_ATTEMPTS:
            return transfer
        ui.hint(
            f"{path.name} will continue after reconnecting "
            f"(retry {attempt} of {MAX_SEND_ATTEMPTS - 1})."
        )
        if not await session.wait_ready(after=generation, timeout=RECONNECT_TIMEOUT + 30):
            return transfer
    return None


async def _send_files(link: Link, session: Session, paths: list[Path], ui: Ui) -> bool:
    connection = asyncio.create_task(_stay_connected(link, session, ui))
    try:
        if not await session.wait_ready(after=-1, timeout=_HELLO_TIMEOUT):
            ui.error("The other side connected but never responded.")
            return False
        if not session.peer_can(Capability.FILE_TRANSFER):
            ui.error("The other side doesn't support file transfer.")
            return False

        ok = True
        for i, path in enumerate(paths, 1):
            if len(paths) > 1:
                ui.hint(f"File {i} of {len(paths)}")
            transfer = await _send_with_retry(session, path, ui)
            ok &= transfer is not None and transfer.status is TransferStatus.COMPLETED
            if connection.done():
                break  # the other side is gone; the rest can't go
        return ok
    finally:
        session.say_bye()
        await asyncio.sleep(0.2)  # let the bye leave before the connection closes
        connection.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await connection


async def _receive_files(link: Link, session: Session, ui: Ui) -> bool:
    ui.ok("Connected peer-to-peer. Waiting for files...")
    await _stay_connected(link, session, ui)  # until the sender says bye or is lost
    transfers = session.transfers.transfers
    if not transfers:
        ui.error("The sender disconnected without sending anything.")
        return False
    # A file may take several transfers (interrupted, then resumed): judge
    # each file by its last attempt
    last: dict[str, Transfer] = {}
    for t in transfers:
        last[t.filename] = t
    return all(t.status is TransferStatus.COMPLETED for t in last.values())


# ── Entrypoint ──────────────────────────────────────────────────────


async def _run(args: argparse.Namespace, server: str, ui: Ui) -> bool:
    is_creator = args.cmd in ("create", "send")

    if is_creator:
        room_id = create_room(server)
        joiner = "join" if args.cmd == "create" else "receive"
        ui.line(f"Room created: {ui.style.bold(room_id)}")
        ui.line(f"On the other machine run:  {ui.style.accent(f'whoosh {joiner} {room_id}')}")
        if server != DEFAULT_SERVER:
            ui.hint(f"(or open {server}/#{room_id} in a browser)")
        ui.hint("Waiting for the other person...")
    else:
        room_id = args.room.code
        ui.hint(f"Joining room {room_id}...")

    link = Link(server, room_id, is_creator=is_creator)
    background: set[asyncio.Task[None]] = set()

    def drop_stalled() -> None:
        # A transfer stalled: the connection is dead even if it hasn't said so
        task = asyncio.create_task(link.drop())
        background.add(task)
        task.add_done_callback(background.discard)

    try:
        peer = await link.open()
        out = Path(getattr(args, "out", ".")).expanduser()
        session = Session(peer, out, ui, on_stall=drop_stalled)
        session.start()

        if args.cmd in ("create", "join"):
            await _interactive(link, session, ui)
            return True
        if args.cmd == "send":
            return await _send_files(link, session, args.files, ui)
        return await _receive_files(link, session, ui)
    finally:
        await link.close()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="whoosh",
        description="Peer-to-peer chat and file transfer.",
        epilog="Examples:\n  whoosh send report.pdf\n  whoosh receive a1b2c3d4\n"
        "  whoosh receive https://whoosh.example/#a1b2c3d4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--server",
        help=f"Signaling server URL (default: $WHOOSH_SERVER or {DEFAULT_SERVER})",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    out_help = "Directory for received files (default: current directory)"
    room_help = "Room code or invite link"
    c = sub.add_parser("create", help="Create a room and chat")
    c.add_argument("--out", default=".", help=out_help)
    j = sub.add_parser("join", help="Join a room and chat")
    j.add_argument("room", type=room_ref, help=room_help)
    j.add_argument("--out", default=".", help=out_help)
    s = sub.add_parser("send", help="Create a room, send files, exit")
    s.add_argument("files", nargs="+", type=Path)
    r = sub.add_parser("receive", help="Join a room, save incoming files, exit")
    r.add_argument("room", type=room_ref, help=room_help)
    r.add_argument("--out", default=".", help=out_help)
    return p


def main() -> None:
    p = _parser()
    args = p.parse_args()
    if args.cmd is None:
        p.print_help()
        sys.exit(2)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING, format="%(message)s"
    )
    # Our own loggers at INFO so "Peer joined — connecting..." shows; libraries stay quiet
    logging.getLogger("apps").setLevel(level)
    # aioice logs every ICE candidate-pair check at INFO — dozens of lines per connect
    logging.getLogger("aioice").setLevel(logging.WARNING)

    if args.cmd == "send":
        missing = [f for f in args.files if not f.is_file()]
        if missing:
            p.error(f"not a file: {', '.join(map(str, missing))}")

    # Precedence: --server, then a server from an invite link, then the environment
    room = getattr(args, "room", None)
    server = (
        args.server
        or (room.server if room else None)
        or os.environ.get("WHOOSH_SERVER")
        or DEFAULT_SERVER
    ).rstrip("/")

    ui = Ui()
    try:
        ok = asyncio.run(_run(args, server, ui))
    except ConnectError as exc:
        ui.error(str(exc))
        sys.exit(1)
    # Network failures talking to the signaling server: unreachable, TLS,
    # HTTP errors (URLError is an OSError), rejected WebSocket handshake
    except (OSError, WebSocketException) as exc:
        ui.error(f"Can't reach the signaling server at {server}: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        ui.hint("\nInterrupted.")
        sys.exit(130)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
