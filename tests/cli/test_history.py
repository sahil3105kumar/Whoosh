"""Local transfer history (#22)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from apps.cli import history
from packages.transfer.manager import Direction, Transfer, TransferStatus

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def history_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "history.jsonl"
    monkeypatch.setenv("WHOOSH_HISTORY_FILE", str(path))
    monkeypatch.delenv("WHOOSH_NO_HISTORY", raising=False)
    return path


async def _transfer(status: TransferStatus = TransferStatus.COMPLETED, **kw) -> Transfer:  # type: ignore[no-untyped-def]
    t = Transfer(
        id="t", direction=Direction.RECEIVE, filename="a.txt", size=10, path=Path("/x/a.txt"), **kw
    )
    t.status = status
    return t


async def test_records_finished_transfers():
    history.record(await _transfer(sha256="ab" * 32))
    history.record(await _transfer(TransferStatus.FAILED, reason="checksum mismatch"))

    entries = history.load()
    assert [e.status for e in entries] == ["completed", "failed"]
    assert entries[0].sha256 == "ab" * 32
    assert entries[0].path == "/x/a.txt"
    assert entries[1].reason == "checksum mismatch"


async def test_ignores_unfinished_transfers():
    history.record(await _transfer(TransferStatus.STREAMING))
    assert history.load() == []


async def test_can_be_disabled(monkeypatch: pytest.MonkeyPatch, history_file: Path):
    monkeypatch.setenv("WHOOSH_NO_HISTORY", "1")
    history.record(await _transfer())
    assert not history_file.exists()


async def test_clear_and_trim(monkeypatch: pytest.MonkeyPatch, history_file: Path):
    monkeypatch.setattr(history, "MAX_ENTRIES", 3)
    for _ in range(5):
        history.record(await _transfer())
    assert len(history.load()) == 3

    history.clear()
    assert history.load() == []


def test_skips_corrupt_lines(history_file: Path):
    history_file.write_text('not json\n{"also": "wrong"}\n')
    assert history.load() == []


def test_unwritable_location_is_harmless(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("WHOOSH_HISTORY_FILE", str(blocker / "history.jsonl"))  # parent is a file
    history.record(_sync_transfer())  # must not raise


def _sync_transfer() -> Transfer:
    async def make() -> Transfer:
        return await _transfer()

    return asyncio.run(make())


def test_history_command(history_file: Path):
    def run(*args: str) -> str:
        env = {"WHOOSH_HISTORY_FILE": str(history_file), "PATH": "/usr/bin:/bin"}
        out = subprocess.run(
            [sys.executable, "-m", "apps.cli.main", "history", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout

    assert "No transfers yet" in run()
    history.record(_sync_transfer())
    assert "a.txt" in run()
    assert "cleared" in run("--clear")
    assert not history_file.exists()
