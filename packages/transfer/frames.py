"""
Binary frame format for file data on the DataChannel.

Text frames carry JSON protocol messages; binary frames carry file
chunks. A chunk frame is a fixed 25-byte header followed by raw data:

    offset  size  field
    0       1     kind         0x01 = file chunk
    1       16    transfer_id  UUID bytes (the id from file_metadata)
    17      8     offset       byte offset of this chunk in the file, big-endian
    25      ...   data

``offset`` rather than a chunk index so resume (v2.0) can restart from
any byte without both sides agreeing on a chunk size.
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass

#: Frame kind for file chunks. Other kinds are reserved for later use.
KIND_CHUNK = 0x01

#: File bytes per chunk. 16 KiB keeps every frame comfortably under the
#: message-size limits of all DataChannel implementations.
CHUNK_SIZE = 16 * 1024

_HEADER = struct.Struct("!B16sQ")
HEADER_SIZE = _HEADER.size


class FrameDecodeError(ValueError):
    """Raised when a binary frame is malformed or of an unknown kind."""


@dataclass(frozen=True)
class ChunkFrame:
    transfer_id: str  # uuid hex, same form as FileMetadataPayload.transfer_id
    offset: int
    data: bytes


def encode_chunk(transfer_id: str, offset: int, data: bytes) -> bytes:
    return _HEADER.pack(KIND_CHUNK, uuid.UUID(transfer_id).bytes, offset) + data


def decode_frame(frame: bytes) -> ChunkFrame:
    if len(frame) < HEADER_SIZE:
        raise FrameDecodeError(f"frame too short ({len(frame)} bytes)")
    kind, id_bytes, offset = _HEADER.unpack_from(frame)
    if kind != KIND_CHUNK:
        raise FrameDecodeError(f"unknown frame kind 0x{kind:02x}")
    return ChunkFrame(
        transfer_id=uuid.UUID(bytes=id_bytes).hex,
        offset=offset,
        data=frame[HEADER_SIZE:],
    )
