"""
Local transfer history (#22, docs/decisions.md D54).

One JSON object per line in ``$XDG_STATE_HOME/whoosh/history.jsonl``
(default ``~/.local/state/whoosh/history.jsonl``). Kept on this machine
only — the server never sees it. ``WHOOSH_HISTORY_FILE`` moves it;
``WHOOSH_NO_HISTORY=1`` turns it off.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from packages.transfer.manager import Transfer

log = logging.getLogger(__name__)

#: The file is trimmed to the newest entries once it grows past this.
MAX_ENTRIES = 1000


@dataclass(frozen=True)
class Entry:
    time: str  # ISO 8601, UTC
    direction: str  # "send" | "receive"
    filename: str
    size: int
    status: str  # "completed" | "failed" | "cancelled"
    reason: str
    #: Source file (send) or where it was saved (receive).
    path: str
    sha256: str
    #: Bytes that were already in place from an earlier attempt.
    resumed_from: int


def history_path() -> Path:
    if explicit := os.environ.get("WHOOSH_HISTORY_FILE"):
        return Path(explicit).expanduser()
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "whoosh" / "history.jsonl"


def enabled() -> bool:
    return os.environ.get("WHOOSH_NO_HISTORY", "") in ("", "0")


def record(transfer: Transfer, *, path: Path | None = None) -> None:
    """Append a finished transfer. Never raises: history is a convenience."""
    if not enabled() or not transfer.status.finished:
        return
    entry = Entry(
        time=datetime.now(UTC).isoformat(timespec="seconds"),
        direction=str(transfer.direction),
        filename=transfer.filename,
        size=transfer.size,
        status=str(transfer.status),
        reason=transfer.reason,
        path=str(transfer.path),
        sha256=transfer.sha256,
        resumed_from=transfer.resumed_from,
    )
    target = path or history_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry)) + "\n")
        _trim(target)
    except OSError as exc:
        log.debug("couldn't write history: %s", exc)


def load(path: Path | None = None) -> list[Entry]:
    """All entries, oldest first. Unreadable lines are skipped."""
    target = path or history_path()
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        with contextlib.suppress(ValueError, TypeError):
            entries.append(Entry(**json.loads(line)))
    return entries


def clear(path: Path | None = None) -> None:
    (path or history_path()).unlink(missing_ok=True)


def _trim(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) > MAX_ENTRIES:
        path.write_text("\n".join(lines[-MAX_ENTRIES:]) + "\n", encoding="utf-8")
