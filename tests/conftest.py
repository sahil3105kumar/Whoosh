from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests (and the CLI processes they start, which inherit the
    environment) out of the developer's real transfer history. History's
    own tests turn it back on."""
    monkeypatch.setenv("WHOOSH_NO_HISTORY", "1")


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Device identities and trusted devices (#30) go to a per-test folder,
    never the developer's real one."""
    monkeypatch.setenv("WHOOSH_DATA_DIR", str(tmp_path / "whoosh-data"))
