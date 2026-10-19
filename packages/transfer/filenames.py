"""
Turning a peer-supplied filename into a safe local path.

The name in file_metadata comes from the other peer and must never
decide *where* a file lands — only what it's called inside the
download directory.
"""

from __future__ import annotations

import re
from pathlib import Path

# Path separators, characters Windows forbids, and control characters.
_UNSAFE = re.compile(r'[\\/<>:"|?*\x00-\x1f\x7f]')
_MAX_NAME_BYTES = 255
_FALLBACK = "download"
PART_SUFFIX = ".part"


def safe_filename(name: str) -> str:
    """Reduce ``name`` to a plain filename with no directory components."""
    # Keep only the last path component, whichever separator the peer's OS uses
    name = re.split(r"[\\/]", name)[-1]
    name = _UNSAFE.sub("_", name).strip().strip(".")
    if not name:
        return _FALLBACK

    if len(name.encode()) > _MAX_NAME_BYTES:
        path = Path(name)
        suffix = path.suffix if len(path.suffix.encode()) < 32 else ""
        stem = path.stem if suffix else name
        budget = _MAX_NAME_BYTES - len(suffix.encode())
        stem = stem.encode()[:budget].decode(errors="ignore")
        name = stem + suffix
    return name


def unique_path(directory: Path, filename: str) -> Path:
    """``directory/filename``, or ``name (1).ext``, ``name (2).ext``, ...
    if that file (or its in-progress .part) already exists."""
    candidate = directory / filename
    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 1
    while candidate.exists() or part_path(candidate).exists():
        candidate = directory / f"{stem} ({n}){suffix}"
        n += 1
    return candidate


def part_path(path: Path) -> Path:
    return path.with_name(path.name + PART_SUFFIX)
