from __future__ import annotations

import pytest

from apps.server import main


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Server tests all share one client IP; don't let them rate-limit each other."""
    main.room_create_limiter.reset()
