"""Unit tests for CLI helpers: arguments, errors, terminal output, queue."""

from __future__ import annotations

import argparse
import asyncio
import io
from pathlib import Path

import pytest

from apps.cli.connection import explain_close
from apps.cli.main import RoomRef, room_ref
from apps.cli.queue import SendQueue
from apps.cli.session import Ui, format_duration, progress_bar

# ── Arguments ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a1b2c3d4", RoomRef("a1b2c3d4")),
        (" A1B2C3D4 ", RoomRef("a1b2c3d4")),
        ("https://whoosh.example/#a1b2c3d4", RoomRef("a1b2c3d4", "https://whoosh.example")),
        ("http://localhost:8000/#DEADBEEF", RoomRef("deadbeef", "http://localhost:8000")),
    ],
)
def test_room_ref_accepts_codes_and_links(value: str, expected: RoomRef):
    assert room_ref(value) == expected


@pytest.mark.parametrize(
    "value", ["", "abc", "a1b2c3d4e", "zzzzzzzz", "https://x.example/", "ftp://x/#a1b2c3d4"]
)
def test_room_ref_rejects_everything_else(value: str):
    with pytest.raises(argparse.ArgumentTypeError):
        room_ref(value)


# ── Errors ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reason", "fragment"),
    [
        ("No such room: 'a1b2c3d4'", "doesn't exist"),
        ("Room expired: 'a1b2c3d4'", "has expired"),
        ("Room is full: 'a1b2c3d4'", "already has two people"),
        ("origin not allowed", "origin not allowed"),
    ],
)
def test_explain_close(reason: str, fragment: str):
    assert fragment in explain_close(reason, "a1b2c3d4")


# ── Terminal output ─────────────────────────────────────────────────


def test_no_colour_when_not_a_terminal():
    out = io.StringIO()
    ui = Ui(out)
    ui.ok("done")
    ui.error("broken")
    assert out.getvalue() == "✓ done\n✗ broken\n"


def test_no_colour_respects_no_color(monkeypatch: pytest.MonkeyPatch):
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("NO_COLOR", "1")
    assert not Ui(Tty()).style.enabled
    monkeypatch.delenv("NO_COLOR")
    assert Ui(Tty()).style.enabled


@pytest.mark.parametrize(
    ("fraction", "bar"), [(0, "░░░░"), (0.5, "██░░"), (1, "████"), (2, "████"), (-1, "░░░░")]
)
def test_progress_bar(fraction: float, bar: str):
    assert progress_bar(fraction, 4) == bar


@pytest.mark.parametrize(
    ("seconds", "text"), [(0.2, "<1s"), (5.1, "6s"), (75, "1m 15s"), (3700, "1h 1m")]
)
def test_format_duration(seconds: float, text: str):
    assert format_duration(seconds) == text


# ── Send queue ──────────────────────────────────────────────────────


async def test_queue_sends_one_at_a_time_in_order():
    started: list[str] = []
    gates: list[asyncio.Event] = []

    async def send(path: Path) -> None:
        started.append(path.name)
        gate = asyncio.Event()
        gates.append(gate)
        await gate.wait()

    q = SendQueue(send)
    q.add([Path("a"), Path("b")])
    q.add([Path("c")])
    await asyncio.sleep(0)
    assert started == ["a"]
    assert q.current == Path("a")
    assert q.waiting == [Path("b"), Path("c")]

    for _ in range(3):
        gates[-1].set()
        await asyncio.sleep(0.01)
    assert started == ["a", "b", "c"]
    assert q.current is None
    await q.close()


async def test_queue_clear_drops_waiting_files():
    sent: list[str] = []

    async def send(path: Path) -> None:
        sent.append(path.name)

    q = SendQueue(send)
    q.add([Path("a"), Path("b")])
    assert q.clear() == [Path("a"), Path("b")]  # cleared before the worker ran
    await asyncio.sleep(0.01)
    assert sent == []
    await q.close()


async def test_queue_keeps_going_after_a_failed_send():
    sent: list[str] = []

    async def send(path: Path) -> None:
        sent.append(path.name)
        if path.name == "a":
            raise RuntimeError("boom")

    q = SendQueue(send)
    q.add([Path("a"), Path("b")])
    await asyncio.sleep(0.01)
    assert sent == ["a", "b"]
    await q.close()
