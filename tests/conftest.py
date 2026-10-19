from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests (and the CLI processes they start, which inherit the
    environment) out of the developer's real transfer history. History's
    own tests turn it back on."""
    monkeypatch.setenv("WHOOSH_NO_HISTORY", "1")
