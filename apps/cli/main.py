"""
Whoosh CLI — chat and send files peer-to-peer.

Usage:
    whoosh create [--out DIR]           create a room, then chat (/send to send files)
    whoosh join ROOM_ID [--out DIR]     join a room, then chat
    whoosh send FILE [FILE ...]         create a room, send files, exit
    whoosh receive ROOM_ID [--out DIR]  join a room, save incoming files, exit

    Global: --server URL (default $WHOOSH_SERVER, else http://localhost:8000),
            -v for debug logs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import shlex
import sys
import threading
from pathlib import Path

from apps.cli.connection import ConnectError, Connection, connect, create_room
from apps.cli.session import Session, Ui
from packages.protocol.enums import Capability
from packages.transfer.manager import TransferStatus

log = logging.getLogger(__name__)

HELP = """\
Type a message and press Enter to chat. Commands:
  /send PATH [PATH ...]   send files (quote paths with spaces)
  /cancel                 cancel all active transfers
  /ping                   measure round-trip time
  /quit                   leave"""

#: How long to wait for the peer's hello before sending files.
_HELLO_TIMEOUT = 10.0


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


async def _interactive(conn: Connection, session: Session, ui: Ui) -> None:
    ui.line("✓ Connected peer-to-peer. Type /help for commands.")
    inbound = asyncio.create_task(session.run_inbound())
    commands = asyncio.create_task(_command_loop(session, ui))
    tasks: set[asyncio.Task[None]] = {inbound, commands, conn.signaling}

    try:
        while True:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # re-raise failures
            if inbound in done:
                ui.line("Peer disconnected.")
                return
            if commands in done:
                return
            # Only signaling finished: the server went away, P2P is unaffected
            tasks.discard(conn.signaling)
    finally:
        for transfer in session.transfers.transfers:
            session.transfers.cancel(transfer.id, "peer left")
        for task in (inbound, commands):
            task.cancel()


async def _command_loop(session: Session, ui: Ui) -> None:
    lines = _stdin_lines()
    sends: set[asyncio.Task[None]] = set()

    while (line := await lines.get()) is not None:
        line = line.strip()
        if not line:
            continue
        if not line.startswith("/"):
            session.send_chat(line)
            continue

        cmd, _, arg = line.partition(" ")
        if cmd == "/quit":
            return
        elif cmd == "/help":
            ui.line(HELP)
        elif cmd == "/ping":
            session.ping()
        elif cmd == "/cancel":
            cancelled = [t for t in session.transfers.transfers if session.transfers.cancel(t.id)]
            if not cancelled:
                ui.line("No active transfers.")
        elif cmd == "/send":
            if not session.peer_can(Capability.FILE_TRANSFER):
                ui.line("Peer doesn't support file transfer.")
                continue
            for path in _parse_paths(arg, ui):
                task = asyncio.create_task(_send_one(session, path, ui))
                sends.add(task)
                task.add_done_callback(sends.discard)
        else:
            ui.line(f"Unknown command {cmd}. Type /help.")


def _parse_paths(arg: str, ui: Ui) -> list[Path]:
    try:
        raw = shlex.split(arg)
    except ValueError as exc:
        ui.line(f"Can't parse paths: {exc}")
        return []
    if not raw:
        ui.line("Usage: /send PATH [PATH ...]")
    paths = []
    for item in raw:
        path = Path(item).expanduser()
        if path.is_file():
            paths.append(path)
        else:
            ui.line(f"Not a file: {path}")
    return paths


async def _send_one(session: Session, path: Path, ui: Ui) -> None:
    try:
        await session.transfers.send_file(path)
    except OSError as exc:
        ui.line(f"✗ {path}: {exc}")


async def _send_files(session: Session, paths: list[Path], ui: Ui) -> bool:
    inbound = asyncio.create_task(session.run_inbound())
    try:
        try:
            await asyncio.wait_for(asyncio.shield(session.peer_hello), _HELLO_TIMEOUT)
        except TimeoutError:
            ui.line("Peer never said hello.")
            return False
        if not session.peer_can(Capability.FILE_TRANSFER):
            ui.line("Peer doesn't support file transfer.")
            return False

        ok = True
        for path in paths:
            transfer = await session.transfers.send_file(path)
            ok &= transfer.status is TransferStatus.COMPLETED
            if inbound.done():
                break  # channel closed; remaining files can't go
        return ok
    finally:
        inbound.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await inbound


async def _receive_files(session: Session, ui: Ui) -> bool:
    ui.line("✓ Connected peer-to-peer. Waiting for files...")
    await session.run_inbound()  # until the sender closes the connection
    transfers = session.transfers.transfers
    if not transfers:
        ui.line("Sender disconnected without sending anything.")
    return bool(transfers) and all(t.status is TransferStatus.COMPLETED for t in transfers)


# ── Entrypoint ──────────────────────────────────────────────────────


async def _run(args: argparse.Namespace) -> bool:
    ui = Ui()
    is_creator = args.cmd in ("create", "send")

    if is_creator:
        room_id = create_room(args.server)
        ui.line(f"Room created: {room_id}")
        joiner = "join" if args.cmd == "create" else "receive"
        ui.line(f"On the other machine run:  whoosh {joiner} {room_id}")
        ui.line("Waiting for peer...")
    else:
        room_id = args.room_id
        ui.line(f"Joining room {room_id}...")

    async with connect(args.server, room_id, is_creator=is_creator) as conn:
        out = Path(getattr(args, "out", ".")).expanduser()
        session = Session(conn.peer, out, ui)
        session.start()

        if args.cmd in ("create", "join"):
            await _interactive(conn, session, ui)
            return True
        if args.cmd == "send":
            return await _send_files(session, args.files, ui)
        return await _receive_files(session, ui)


def main() -> None:
    p = argparse.ArgumentParser(prog="whoosh", description="Peer-to-peer chat and file transfer")
    p.add_argument(
        "--server",
        default=os.environ.get("WHOOSH_SERVER", "http://localhost:8000"),
        help="Signaling server URL (default: $WHOOSH_SERVER or http://localhost:8000)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    out_help = "Directory for received files (default: current directory)"
    c = sub.add_parser("create", help="Create a room and chat")
    c.add_argument("--out", default=".", help=out_help)
    j = sub.add_parser("join", help="Join a room and chat")
    j.add_argument("room_id")
    j.add_argument("--out", default=".", help=out_help)
    s = sub.add_parser("send", help="Create a room, send files, exit")
    s.add_argument("files", nargs="+", type=Path)
    r = sub.add_parser("receive", help="Join a room, save incoming files, exit")
    r.add_argument("room_id")
    r.add_argument("--out", default=".", help=out_help)

    args = p.parse_args()

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

    try:
        ok = asyncio.run(_run(args))
    except ConnectError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
