"""
File transfer over a DataChannel, in both directions.

Wire sequence for one file (sender → receiver unless noted):

    file_metadata        JSON   id, name, size, mime type
    chunk frames         binary see frames.py, strictly in offset order
    transfer_complete    JSON   sha256 of the whole file
    transfer_complete    JSON   receiver → sender, once verified and saved

Either side may send ``transfer_cancel`` (with a reason) at any point;
that ends the transfer for both. The channel is reliable and ordered,
so chunks need no acknowledgement in v1.

This module knows nothing about aiortc: it talks to anything with
``send()`` and ``drain()`` (see ``Channel``), which WebRTCPeer provides.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import mimetypes
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import IO, Protocol

from packages.protocol.codec import encode
from packages.protocol.enums import MessageType
from packages.protocol.models import (
    FileMetadataPayload,
    Payload,
    ProtocolMessage,
    TransferCancelPayload,
    TransferCompletePayload,
    make_message,
)
from packages.transfer.filenames import part_path, safe_filename, unique_path
from packages.transfer.frames import CHUNK_SIZE, decode_frame, encode_chunk

logger = logging.getLogger(__name__)

#: How long a sender waits for the receiver to confirm a fully sent file.
#: Covers draining the last buffered bytes plus hashing on the far side.
DEFAULT_CONFIRM_TIMEOUT = 60.0

#: Minimum seconds between progress callbacks for one transfer.
_REPORT_INTERVAL = 0.1


class Channel(Protocol):
    def send(self, data: str | bytes) -> None: ...

    async def drain(self) -> None: ...


class Direction(StrEnum):
    SEND = "send"
    RECEIVE = "receive"


class TransferStatus(StrEnum):
    STREAMING = "streaming"
    COMPLETED = "completed"
    CANCELLED = "cancelled"  # stopped deliberately, by either side
    FAILED = "failed"  # stopped by an error

    @property
    def finished(self) -> bool:
        return self is not TransferStatus.STREAMING


@dataclass(eq=False)
class Transfer:
    id: str
    direction: Direction
    filename: str
    size: int
    #: Source file when sending; destination when receiving (final
    #: name once completed — data goes to a .part file until then).
    path: Path
    bytes_done: int = 0
    status: TransferStatus = TransferStatus.STREAMING
    reason: str = ""

    _done: asyncio.Future[None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future(), repr=False
    )
    _hash: hashlib._Hash = field(default_factory=hashlib.sha256, repr=False)
    _file: IO[bytes] | None = field(default=None, repr=False)
    _last_report: float = field(default=0.0, repr=False)


class TransferManager:
    def __init__(
        self,
        channel: Channel,
        download_dir: Path,
        *,
        on_update: Callable[[Transfer], None] | None = None,
        confirm_timeout: float = DEFAULT_CONFIRM_TIMEOUT,
    ) -> None:
        self._channel = channel
        self._download_dir = download_dir
        self._on_update = on_update
        self._confirm_timeout = confirm_timeout
        self._transfers: dict[str, Transfer] = {}

    @property
    def transfers(self) -> list[Transfer]:
        return list(self._transfers.values())

    # ── Sending ─────────────────────────────────────────────────────

    async def send_file(self, path: Path) -> Transfer:
        """Stream ``path`` to the peer and wait for its confirmation.

        Returns the finished Transfer; check ``status`` for the outcome.
        Raises OSError only if the file can't be opened at all.
        """
        size = path.stat().st_size
        transfer = Transfer(
            id=uuid.uuid4().hex,
            direction=Direction.SEND,
            filename=path.name,
            size=size,
            path=path,
        )
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        with path.open("rb") as f:
            self._transfers[transfer.id] = transfer
            try:
                self._send(
                    MessageType.FILE_METADATA,
                    FileMetadataPayload(
                        transfer_id=transfer.id,
                        filename=transfer.filename,
                        size=size,
                        mime_type=mime_type,
                    ),
                )
                self._report(transfer, force=True)
                await self._stream(transfer, f)

                if not transfer.status.finished:
                    self._send(
                        MessageType.TRANSFER_COMPLETE,
                        TransferCompletePayload(
                            transfer_id=transfer.id, sha256=transfer._hash.hexdigest()
                        ),
                    )
                    await asyncio.wait_for(asyncio.shield(transfer._done), self._confirm_timeout)
            # ConnectionError and TimeoutError are OSError subclasses: order matters
            except ConnectionError:
                self._finish(transfer, TransferStatus.FAILED, "connection lost", notify=False)
            except TimeoutError:
                self._fail(transfer, "receiver did not confirm the file")
            except OSError as exc:
                self._fail(transfer, f"read error: {exc}")

        return transfer

    async def _stream(self, transfer: Transfer, f: IO[bytes]) -> None:
        offset = 0
        while chunk := f.read(CHUNK_SIZE):
            await self._channel.drain()
            # Either side may have cancelled while we waited for the buffer
            if transfer.status.finished:
                return
            self._channel.send(encode_chunk(transfer.id, offset, chunk))
            transfer._hash.update(chunk)
            offset += len(chunk)
            transfer.bytes_done = offset
            self._report(transfer)

        if offset != transfer.size:
            self._fail(transfer, "file changed while it was being sent")

    # ── Receiving ───────────────────────────────────────────────────

    async def handle_message(self, msg: ProtocolMessage) -> bool:
        """Handle a transfer-related protocol message.

        Returns False if ``msg`` isn't one, so the caller can dispatch
        it elsewhere.
        """
        payload = msg.payload
        if isinstance(payload, FileMetadataPayload):
            self._start_receive(payload)
        elif isinstance(payload, TransferCompletePayload):
            self._handle_complete(payload)
        elif isinstance(payload, TransferCancelPayload):
            transfer = self._active(payload.transfer_id)
            if transfer is not None:
                reason = payload.reason or "cancelled by peer"
                self._finish(transfer, TransferStatus.CANCELLED, reason, notify=False)
        else:
            return False
        return True

    def handle_frame(self, frame: bytes) -> None:
        """Handle a binary frame. Raises FrameDecodeError if malformed."""
        chunk = decode_frame(frame)
        transfer = self._active(chunk.transfer_id)
        if transfer is None or transfer.direction is not Direction.RECEIVE:
            return  # e.g. chunks still in flight after a cancel — drop them
        assert transfer._file is not None

        if chunk.offset != transfer.bytes_done:
            self._fail(transfer, f"expected offset {transfer.bytes_done}, got {chunk.offset}")
            return
        if transfer.bytes_done + len(chunk.data) > transfer.size:
            self._fail(transfer, "received more data than announced")
            return

        try:
            transfer._file.write(chunk.data)
        except OSError as exc:
            self._fail(transfer, f"write error: {exc}")
            return
        transfer._hash.update(chunk.data)
        transfer.bytes_done += len(chunk.data)
        self._report(transfer)

    def _start_receive(self, meta: FileMetadataPayload) -> None:
        try:
            transfer_id = uuid.UUID(meta.transfer_id).hex
        except ValueError:
            self._reject(meta.transfer_id, "invalid transfer id")
            return
        if transfer_id in self._transfers:
            self._reject(meta.transfer_id, "duplicate transfer id")
            return
        if meta.size < 0:
            self._reject(meta.transfer_id, "invalid size")
            return

        self._download_dir.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self._download_dir).free < meta.size:
            self._reject(meta.transfer_id, "not enough disk space on receiver")
            return

        filename = safe_filename(meta.filename)
        dest = unique_path(self._download_dir, filename)
        try:
            file = part_path(dest).open("xb")
        except OSError as exc:
            self._reject(meta.transfer_id, f"cannot create file: {exc}")
            return

        transfer = Transfer(
            id=transfer_id,
            direction=Direction.RECEIVE,
            filename=filename,
            size=meta.size,
            path=dest,
            _file=file,
        )
        self._transfers[transfer_id] = transfer
        self._report(transfer, force=True)

    def _handle_complete(self, payload: TransferCompletePayload) -> None:
        transfer = self._active(payload.transfer_id)
        if transfer is None:
            return

        if transfer.direction is Direction.SEND:
            # The receiver's confirmation: verified and saved
            self._finish(transfer, TransferStatus.COMPLETED, notify=False)
            return

        if transfer.bytes_done != transfer.size:
            self._fail(transfer, f"incomplete: got {transfer.bytes_done} of {transfer.size} bytes")
            return
        if transfer._hash.hexdigest() != payload.sha256.lower():
            self._fail(transfer, "checksum mismatch")
            return

        assert transfer._file is not None
        transfer._file.close()
        transfer._file = None
        # The name may have been taken while we were receiving
        final = transfer.path
        if final.exists():
            final = unique_path(final.parent, final.name)
        try:
            part_path(transfer.path).rename(final)
        except OSError as exc:
            self._fail(transfer, f"cannot save file: {exc}")
            return
        transfer.path = final

        self._send(MessageType.TRANSFER_COMPLETE, payload)
        self._finish(transfer, TransferStatus.COMPLETED, notify=False)

    # ── Cancel / teardown ───────────────────────────────────────────

    def cancel(self, transfer_id: str, reason: str = "cancelled by user") -> bool:
        """Cancel an active transfer (either direction). Returns False if
        there's no such active transfer."""
        transfer = self._active(transfer_id)
        if transfer is None:
            return False
        self._finish(transfer, TransferStatus.CANCELLED, reason, notify=True)
        return True

    def close(self, reason: str = "connection closed") -> None:
        """Fail every active transfer without notifying the peer — call
        when the channel is gone."""
        for transfer in list(self._transfers.values()):
            if not transfer.status.finished:
                self._finish(transfer, TransferStatus.FAILED, reason, notify=False)

    # ── Internals ───────────────────────────────────────────────────

    def _active(self, transfer_id: str) -> Transfer | None:
        with contextlib.suppress(ValueError):
            transfer_id = uuid.UUID(transfer_id).hex
        transfer = self._transfers.get(transfer_id)
        if transfer is None or transfer.status.finished:
            return None
        return transfer

    def _fail(self, transfer: Transfer, reason: str) -> None:
        self._finish(transfer, TransferStatus.FAILED, reason, notify=True)

    def _finish(
        self, transfer: Transfer, status: TransferStatus, reason: str = "", *, notify: bool
    ) -> None:
        if transfer.status.finished:
            return
        transfer.status = status
        transfer.reason = reason

        if transfer._file is not None:
            transfer._file.close()
            transfer._file = None
            part_path(transfer.path).unlink(missing_ok=True)

        if notify:
            with contextlib.suppress(ConnectionError):
                self._send(
                    MessageType.TRANSFER_CANCEL,
                    TransferCancelPayload(transfer_id=transfer.id, reason=reason),
                )

        if not transfer._done.done():
            transfer._done.set_result(None)
        self._report(transfer, force=True)

    def _reject(self, transfer_id: str, reason: str) -> None:
        logger.warning("rejecting incoming transfer %s: %s", transfer_id, reason)
        with contextlib.suppress(ConnectionError):
            self._send(
                MessageType.TRANSFER_CANCEL,
                TransferCancelPayload(transfer_id=transfer_id, reason=reason),
            )

    def _send(self, msg_type: MessageType, payload: Payload) -> None:
        self._channel.send(encode(make_message(msg_type, payload)).decode())

    def _report(self, transfer: Transfer, *, force: bool = False) -> None:
        if self._on_update is None:
            return
        now = time.monotonic()
        if force or now - transfer._last_report >= _REPORT_INTERVAL:
            transfer._last_report = now
            self._on_update(transfer)
