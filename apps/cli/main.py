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
import getpass
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

from apps.cli import history
from apps.cli.connection import ConnectError, Mesh, create_room, room_needs_password
from apps.cli.queue import SendQueue
from apps.cli.room import Room
from apps.cli.session import Ui, format_bytes
from packages.transfer.manager import Transfer, TransferStatus

log = logging.getLogger(__name__)

DEFAULT_SERVER = "http://localhost:8000"

HELP = """\
Type a message and press Enter to chat with everyone in the room. Commands:
  /send PATH [PATH ...]   send files to everyone here, one file at a time
                          (quote paths with spaces)
  /queue                  show the file being sent and those waiting
  /cancel                 cancel current transfers and clear the queue
  /peers                  list who's in the room
  /ping                   measure round-trip time to everyone
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


async def _interactive(room: Room, ui: Ui) -> None:
    ui.hint("Type a message and press Enter, /send PATH to send files, /help for more.")
    queue = SendQueue(lambda path: _send_to_all(room, path, ui))
    commands = asyncio.create_task(_command_loop(room, queue, ui))
    try:
        await commands
    finally:
        room.say_bye()
        dropped = queue.clear()
        if dropped:
            ui.hint(f"{len(dropped)} queued file(s) weren't sent.")
        await queue.close()


async def _send_to_all(room: Room, path: Path, ui: Ui) -> None:
    if not room.connected():
        ui.hint(f"Nobody is connected; {path.name} wasn't sent.")
        return
    await room.send_to_all(path)


async def _command_loop(room: Room, queue: SendQueue, ui: Ui) -> None:
    lines = _stdin_lines()

    while (line := await lines.get()) is not None:
        line = line.strip()
        if not line:
            continue
        try:
            if not _command(line, room, queue, ui):
                return
        except ConnectionError:
            ui.hint("Not connected right now (reconnecting); that wasn't sent.")


def _command(line: str, room: Room, queue: SendQueue, ui: Ui) -> bool:
    """Handle one line of input. False means quit."""
    if not line.startswith("/"):
        if not room.broadcast(lambda s: s.send_chat(line)):  # group chat (#24)
            ui.hint("Nobody is connected yet; that wasn't sent.")
        return True

    cmd, _, arg = line.partition(" ")
    if cmd == "/quit":
        return False
    elif cmd == "/help":
        ui.line(HELP)
    elif cmd == "/ping":
        room.broadcast(lambda s: s.ping())
    elif cmd == "/peers":
        _show_peers(room, ui)
    elif cmd == "/queue":
        _show_queue(queue, ui)
    elif cmd == "/cancel":
        dropped = queue.clear()
        cancelled = [
            t
            for s in room.sessions.values()
            for t in s.transfers.transfers
            if s.transfers.cancel(t.id)
        ]
        if not cancelled and not dropped:
            ui.hint("Nothing to cancel.")
        elif dropped:
            ui.hint(f"Removed {len(dropped)} queued file(s).")
    elif cmd == "/send":
        paths = _parse_paths(arg, ui)
        if paths:
            busy = queue.current is not None
            queue.add(paths)
            if busy or len(paths) > 1:
                ui.hint(f"Queued {len(paths)} file(s); /queue to see the list.")
    else:
        ui.error(f"Unknown command {cmd}. Type /help for the list.")
    return True


def _show_peers(room: Room, ui: Ui) -> None:
    if not room.sessions:
        ui.hint("Nobody else is here yet.")
        return
    for session in room.sessions.values():
        state = "connected" if session.connected.is_set() else "reconnecting"
        ui.line(f"  {session.peer_name}  {ui.style.dim(state)}")


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


async def _send_files(room: Room, paths: list[Path], peers: int, ui: Ui) -> bool:
    """Wait for ``peers`` people, send every file to all of them, leave."""
    await room.wait_for_peers(peers)
    ok = True
    try:
        for i, path in enumerate(paths, 1):
            if len(paths) > 1:
                ui.hint(f"File {i} of {len(paths)}")
            recipients = len(room.connected())
            transfers = await room.send_to_all(path)
            ok &= len(transfers) == recipients and all(
                t.status is TransferStatus.COMPLETED for t in transfers
            )
            if not room.connected():
                break  # everyone is gone; the rest can't go
        return ok
    finally:
        room.say_bye()
        await asyncio.sleep(0.2)  # let the bye leave before the connections close


async def _receive_files(room: Room, ui: Ui) -> bool:
    await room.wait_for_peers(1)
    ui.ok("Connected peer-to-peer. Waiting for files...")
    await room.wait_until_empty()  # until the senders say bye or are lost
    transfers = [t for s in room.everyone() for t in s.transfers.transfers]
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

    password = _password(args, confirm=is_creator)
    if is_creator:
        room_id = create_room(server, password=password)
        joiner = "join" if args.cmd == "create" else "receive"
        ui.line(f"Room created: {ui.style.bold(room_id)}")
        ui.line(f"On the other machine run:  {ui.style.accent(f'whoosh {joiner} {room_id}')}")
        if server != DEFAULT_SERVER:
            ui.hint(f"(or open {server}/#{room_id} in a browser)")
        ui.hint("Waiting for the other person...")
        if password:
            ui.hint("The room needs the password you set; share it separately.")
    else:
        room_id = args.room.code
        if password is None and room_needs_password(server, room_id):
            password = _ask_password(f"Password for room {room_id}: ")
        ui.hint(f"Joining room {room_id}...")

    mesh = Mesh(server, room_id, password=password)
    out = Path(getattr(args, "out", ".")).expanduser()
    room = Room(mesh, ui, out, args.name)
    await mesh.start()
    events = asyncio.create_task(room.run())
    try:
        if args.cmd in ("create", "join"):
            if args.cmd == "join":
                await room.wait_for_peers(1)
                ui.ok("Connected peer-to-peer.")
            await _interactive(room, ui)
            return True
        if args.cmd == "send":
            return await _send_files(room, args.files, args.peers, ui)
        return await _receive_files(room, ui)
    finally:
        events.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await events
        await room.close()


_PROMPT = object()  # --password given without a value: ask


def _password(args: argparse.Namespace, *, confirm: bool) -> str | None:
    """The room password from --password / $WHOOSH_PASSWORD, prompting when
    --password was given bare (#28)."""
    if args.password is _PROMPT:
        first = _ask_password("Room password: ")
        if confirm and _ask_password("Again: ") != first:
            raise ConnectError("The passwords didn't match.")
        return first
    return args.password or os.environ.get("WHOOSH_PASSWORD") or None


def _ask_password(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise ConnectError("This room needs a password: pass --password or set WHOOSH_PASSWORD.")
    password = getpass.getpass(prompt)
    if not password:
        raise ConnectError("No password given.")
    return password


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
    p.add_argument("--name", help="Name shown to the other people in the room")
    p.add_argument(
        "--password",
        nargs="?",
        const=_PROMPT,
        metavar="PASSWORD",
        help="Room password: protects a room you create, or opens a protected one. "
        "Give it without a value to be asked (or set $WHOOSH_PASSWORD).",
    )
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
    s.add_argument(
        "--peers",
        type=int,
        default=1,
        metavar="N",
        help="Wait for N people to join, then send to all of them (default 1)",
    )
    r = sub.add_parser("receive", help="Join a room, save incoming files, exit")
    r.add_argument("room", type=room_ref, help=room_help)
    r.add_argument("--out", default=".", help=out_help)
    h = sub.add_parser("history", help="Show past transfers (kept only on this machine)")
    h.add_argument("-n", type=int, default=20, help="How many to show (default 20)")
    h.add_argument("--clear", action="store_true", help="Delete the history")
    return p


def _show_history(args: argparse.Namespace, ui: Ui) -> None:
    if args.clear:
        history.clear()
        ui.ok("History cleared.")
        return
    entries = history.load()[-args.n :]
    if not entries:
        where = "disabled (WHOOSH_NO_HISTORY)" if not history.enabled() else "empty"
        ui.hint(f"No transfers yet; history is {where}.")
        return
    for e in reversed(entries):
        when = e.time.replace("T", " ").split("+")[0]
        arrow = "↑" if e.direction == "send" else "↓"
        if e.status == "completed":
            outcome = ui.style.ok("done")
        else:
            outcome = ui.style.error(f"{e.status}: {e.reason}")
        ui.line(f"{ui.style.dim(when)}  {arrow} {e.filename} ({format_bytes(e.size)})  {outcome}")
        if e.status == "completed":
            ui.line(ui.style.dim(f"    {e.path}  sha256 {e.sha256[:16]}…"))
    ui.hint(f"\n{history.history_path()}")


def main() -> None:
    p = _parser()
    args = p.parse_args()
    if args.cmd is None:
        p.print_help()
        sys.exit(2)
    if args.cmd == "history":
        _show_history(args, Ui())
        return

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
