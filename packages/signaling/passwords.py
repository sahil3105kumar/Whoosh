"""
Room passwords (#28, docs/decisions.md D58): only a salted scrypt hash is
kept, in memory, for the life of the room.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

#: scrypt cost: ~50 ms and 16 MiB per check. Slow enough to make guessing
#: expensive, cheap enough for a join.
_N, _R, _P = 2**14, 8, 1

MAX_LENGTH = 256


@dataclass(frozen=True)
class PasswordHash:
    salt: bytes
    digest: bytes

    @classmethod
    def create(cls, password: str) -> PasswordHash:
        salt = secrets.token_bytes(16)
        return cls(salt, _scrypt(password, salt))

    def matches(self, password: str) -> bool:
        return hmac.compare_digest(self.digest, _scrypt(password, self.salt))


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
