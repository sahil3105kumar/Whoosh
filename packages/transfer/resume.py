"""
Resume support: identifying a file across sessions, and keeping partial
downloads on disk so a later transfer of the same file can continue.

Resume key (must match apps/web/src/lib/resume.ts byte for byte):

    sha256(
        utf8("whoosh-resume-v1\\n{size}\\n{mtime_ms}\\n")
        || file[0 : min(SAMPLE, size)]
        || file[max(0, size - SAMPLE) : size]
    ) as lowercase hex

``mtime_ms`` is the modification time in whole milliseconds. Sampling the
head and tail rather than hashing the whole file keeps this cheap; a
false match can't corrupt anything, because the receiver still verifies
the full-file SHA-256 at the end (docs/decisions.md D46).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
from pathlib import Path
from typing import IO

#: Bytes sampled from each end of the file for the resume key.
SAMPLE = 1024 * 1024

#: Partials untouched for this long are deleted when a manager starts.
PARTIAL_MAX_AGE_SECONDS = 7 * 24 * 3600

_KEY = re.compile(r"^[0-9a-f]{64}$")


def resume_key(path: Path) -> str:
    st = path.stat()
    size = st.st_size
    h = hashlib.sha256(f"whoosh-resume-v1\n{size}\n{st.st_mtime_ns // 1_000_000}\n".encode())
    with path.open("rb") as f:
        h.update(f.read(SAMPLE))
        f.seek(max(0, size - SAMPLE))
        h.update(f.read(SAMPLE))
    return h.hexdigest()


def is_resume_key(value: str) -> bool:
    return bool(_KEY.match(value))


class PartialStore:
    """Partial downloads in ``<download dir>/.whoosh-partial/``.

    Each partial is ``<name>.part`` plus ``<name>.json`` recording the
    announced size. ``name`` is the resume key when the sender supports
    resume, else the transfer id (those are deleted on any failure).
    """

    def __init__(self, download_dir: Path) -> None:
        self.directory = download_dir / ".whoosh-partial"

    def data_path(self, name: str) -> Path:
        return self.directory / f"{name}.part"

    def _meta_path(self, name: str) -> Path:
        return self.directory / f"{name}.json"

    def existing(self, name: str, size: int) -> int:
        """Bytes already held for ``name``, if it's a usable partial of a
        ``size``-byte file; 0 otherwise (and any mismatched leftovers are
        removed)."""
        data, meta = self.data_path(name), self._meta_path(name)
        try:
            recorded = json.loads(meta.read_text())["size"]
            held = data.stat().st_size
        except (OSError, ValueError, KeyError, TypeError):
            self.discard(name)
            return 0
        if recorded != size or held > size:
            self.discard(name)
            return 0
        return held

    def open(self, name: str, size: int, offset: int) -> IO[bytes]:
        """Open for writing at ``offset`` (0 = start a new partial)."""
        self.directory.mkdir(parents=True, exist_ok=True)
        if offset == 0:
            self._meta_path(name).write_text(json.dumps({"size": size}))
            return self.data_path(name).open("wb")
        file = self.data_path(name).open("r+b")
        file.seek(offset)
        file.truncate()
        return file

    def hash_prefix(self, name: str, offset: int, hasher: hashlib._Hash) -> None:
        """Feed the first ``offset`` bytes of a partial into ``hasher``."""
        with self.data_path(name).open("rb") as f:
            remaining = offset
            while remaining:
                block = f.read(min(remaining, SAMPLE))
                if not block:
                    raise OSError("partial file shorter than expected")
                hasher.update(block)
                remaining -= len(block)

    def finish(self, name: str, dest: Path) -> None:
        """Move a completed partial to ``dest``."""
        self.data_path(name).rename(dest)
        self._meta_path(name).unlink(missing_ok=True)
        self._remove_dir_if_empty()

    def discard(self, name: str) -> None:
        self.data_path(name).unlink(missing_ok=True)
        self._meta_path(name).unlink(missing_ok=True)
        self._remove_dir_if_empty()

    def _remove_dir_if_empty(self) -> None:
        # Don't leave a stray hidden folder in the user's downloads
        with contextlib.suppress(OSError):
            self.directory.rmdir()  # fails (harmlessly) unless empty

    def cleanup(self, max_age: float = PARTIAL_MAX_AGE_SECONDS) -> None:
        """Delete partials nobody has resumed in ``max_age`` seconds."""
        if not self.directory.is_dir():
            return
        cutoff = time.time() - max_age
        for path in self.directory.iterdir():
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
