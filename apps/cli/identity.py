"""
This device's identity and the devices it has verified (#30, D61).

Files in ``$XDG_DATA_HOME/whoosh/`` (default ``~/.local/share/whoosh/``),
or ``$WHOOSH_DATA_DIR``:

* ``identity.key`` — this device's Ed25519 private key (mode 0600).
  Losing it just means peers see a new, unverified device.
* ``trusted.json`` — fingerprints of devices verified by comparing codes,
  with the name they had at the time.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from packages.crypto.e2e import Identity


def data_dir() -> Path:
    if explicit := os.environ.get("WHOOSH_DATA_DIR"):
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "whoosh"


def load_identity() -> Identity:
    """This device's identity, created on first use."""
    path = data_dir() / "identity.key"
    try:
        return Identity.from_bytes(path.read_bytes())
    except (OSError, ValueError):
        pass
    identity = Identity.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created with owner-only permissions from the start, never world-readable
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(identity.to_bytes())
    return identity


class TrustStore:
    """Devices this user has verified."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or data_dir() / "trusted.json"
        try:
            self._entries: dict[str, dict[str, str]] = json.loads(self._path.read_text())
        except (OSError, ValueError):
            self._entries = {}

    def is_verified(self, fingerprint: str) -> bool:
        return fingerprint in self._entries

    def verify(self, fingerprint: str, name: str) -> None:
        self._entries[fingerprint] = {
            "name": name,
            "verified": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self._save()

    def forget(self, fingerprint: str) -> None:
        if self._entries.pop(fingerprint, None) is not None:
            self._save()

    def verified_as(self, name: str) -> list[str]:
        """Fingerprints verified under ``name`` (to spot a familiar name on
        a different device)."""
        return [fp for fp, e in self._entries.items() if e.get("name") == name]

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._entries, indent=2))
        tmp.replace(self._path)
