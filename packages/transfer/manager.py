"""
File transfer over a DataChannel, in both directions.

Wire sequence for one file (sender → receiver unless noted):

    file_metadata        JSON   id, name, size, mime type, [resume_key]
    transfer_accept      JSON   receiver → sender, only if resume_key was
                                sent: the offset to start from
    chunk frames         binary see frames.py, strictly in offset order
    transfer_ack         JSON   receiver → sender, every ACK_EVERY bytes
                                stored, only if file_metadata asked
    transfer_complete    JSON   sha256 of the whole file
    transfer_complete    JSON   receiver → sender, once verified and saved

Either side may send ``transfer_cancel`` (with a reason) at any point;
that ends the transfer for both. The channel is reliable and ordered,
so chunks need no acknowledgement.

Resume (docs/decisions.md D46–D47): when the peer advertised the
``resume`` capability, the sender includes a ``resume_key``. A receiver
holding a partial copy under that key answers with its length and the
sender streams only the rest. Partials survive a lost connection and
are deleted on cancel or on any integrity failure.

Acknowledgements (D49): with ``ack``, the sender's progress counts
bytes the receiver has stored, and at most ACK_WINDOW bytes may be
unacknowledged. No ack for STALL_TIMEOUT while the window is full means
the connection is dead even if it hasn't said so.

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
from typing import IO, Any, Protocol

from packages.protocol.codec import encode
from packages.protocol.enums import MessageType
from packages.protocol.models import (
    FileMetadataPayload,
    Payload,
    ProtocolMessage,
    TransferAcceptPayload,
    TransferAckPayload,
    TransferCancelPayload,
    TransferCompletePayload,
    make_message,
)
from packages.transfer.chunking import ChunkSizer
from packages.transfer.filenames import safe_filename, unique_path
from packages.transfer.frames import decode_frame, encode_chunk
from packages.transfer.resume import SAMPLE, PartialStore, is_resume_key, resume_key

logger = logging.getLogger(__name__)

#: How long a sender waits for the receiver to confirm a fully sent file.
#: Covers draining the last buffered bytes plus hashing on the far side.
DEFAULT_CONFIRM_TIMEOUT = 60.0

#: How long a sender waits for transfer_accept. The receiver may first
#: re-hash a large partial from disk.
DEFAULT_ACCEPT_TIMEOUT = 60.0

#: Receivers acknowledge after storing this many more bytes.
ACK_EVERY = 1024 * 1024

#: Senders keep at most this many bytes unacknowledged (sent, not stored).
ACK_WINDOW = 8 * 1024 * 1024

#: A sender with a full window and no ack for this long gives up: the
#: receiver or the path to it has stopped working.
DEFAULT_STALL_TIMEOUT = 20.0

#: Largest frame assumed when the channel doesn't say: aiortc's limit,
#: which every DataChannel implementation accepts.
DEFAULT_MAX_MESSAGE = 65536

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
    #: name once completed — data goes to a partial file until then).
    path: Path
    bytes_done: int = 0
    #: Bytes that were already in place when this transfer started.
    resumed_from: int = 0
    #: Sender: bytes the receiver confirmed storing. Only meaningful when
    #: ``acked`` (the receiver supports acks); otherwise equals bytes_done.
    bytes_acked: int = 0
    acked: bool = False
    status: TransferStatus = TransferStatus.STREAMING
    reason: str = ""
    #: Failed because the connection was lost or stalled, not because of
    #: the data: worth retrying once reconnected (resume makes it cheap).
    interrupted: bool = False

    _done: asyncio.Future[None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future(), repr=False
    )
    #: Sender: resolved with the receiver's starting offset.
    #: Sender: set whenever a transfer_ack arrives.
    _ack_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    #: Receiver: bytes covered by the last ack sent, and whether to send them.
    _last_ack_sent: int = field(default=0, repr=False)
    _send_acks: bool = field(default=False, repr=False)
    _accepted: asyncio.Future[int] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future(), repr=False
    )
    _hash: hashlib._Hash = field(default_factory=hashlib.sha256, repr=False)
    _file: IO[bytes] | None = field(default=None, repr=False)
    #: Receiver: name of the partial in the PartialStore, and whether it
    #: may outlive a lost connection (only resumable ones do).
    _partial: str | None = field(default=None, repr=False)
    _resumable: bool = field(default=False, repr=False)
    _last_report: float = field(default=0.0, repr=False)


class TransferManager:
    def __init__(
        self,
        channel: Channel,
        download_dir: Path,
        *,
        on_update: Callable[[Transfer], None] | None = None,
        on_stall: Callable[[], None] | None = None,
        confirm_timeout: float = DEFAULT_CONFIRM_TIMEOUT,
        accept_timeout: float = DEFAULT_ACCEPT_TIMEOUT,
        stall_timeout: float = DEFAULT_STALL_TIMEOUT,
    ) -> None:
        self._channel = channel
        self._download_dir = download_dir
        self._partials = PartialStore(download_dir)
        self._on_update = on_update
        self._on_stall = on_stall
        self._confirm_timeout = confirm_timeout
        self._accept_timeout = accept_timeout
        self._stall_timeout = stall_timeout
        self._transfers: dict[str, Transfer] = {}
        with contextlib.suppress(OSError):
            self._partials.cleanup()

    @property
    def transfers(self) -> list[Transfer]:
        return list(self._transfers.values())

    def attach(self, channel: Channel) -> None:
        """Use a new channel after a reconnect. Transfers on the old one
        have already ended (``close``); they continue by being re-sent."""
        self._channel = channel

    # ── Sending ─────────────────────────────────────────────────────

    async def send_file(self, path: Path, *, resume: bool = False, ack: bool = False) -> Transfer:
        """Stream ``path`` to the peer and wait for its confirmation.

        ``resume``: the peer advertised the resume capability, so offer a
        resume key and start wherever the receiver says.
        ``ack``: the peer advertised acks, so ask for them, report
        acknowledged progress and keep a bounded window in flight.

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
            acked=ack,
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
                        resume_key=resume_key(path) if resume else None,
                        ack=ack,
                    ),
                )
                offset = await self._await_accept(transfer) if resume else 0
                if transfer.status.finished:
                    return transfer
                if offset:
                    self._skip_prefix(transfer, f, offset)
                self._report(transfer, force=True)
                await self._stream(transfer, f, offset)

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
                self._interrupt(transfer, "connection lost")
            except TimeoutError:
                self._fail(transfer, "receiver did not confirm the file")
            except OSError as exc:
                self._fail(transfer, f"read error: {exc}")

        return transfer

    async def _await_accept(self, transfer: Transfer) -> int:
        """Wait for transfer_accept (or for the transfer to end first)."""
        waiters: set[asyncio.Future[Any]] = {transfer._accepted, transfer._done}
        done, _ = await asyncio.wait(
            waiters, timeout=self._accept_timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            self._fail(transfer, "receiver did not answer the transfer offer")
            return 0
        if transfer.status.finished:
            return 0
        offset = transfer._accepted.result()
        if not 0 <= offset <= transfer.size:
            self._fail(transfer, f"receiver asked for an invalid offset {offset}")
            return 0
        return offset

    def _skip_prefix(self, transfer: Transfer, f: IO[bytes], offset: int) -> None:
        """Hash the part the receiver already has, without sending it: the
        final sha256 covers the whole file."""
        remaining = offset
        while remaining:
            block = f.read(min(remaining, SAMPLE))
            if not block:
                raise OSError("file shrank since the transfer was offered")
            transfer._hash.update(block)
            remaining -= len(block)
        transfer.bytes_done = transfer.resumed_from = transfer.bytes_acked = offset

    async def _stream(self, transfer: Transfer, f: IO[bytes], offset: int) -> None:
        # Adaptive chunk size, capped by what the connection can carry (D52)
        sizer = ChunkSizer(getattr(self._channel, "max_message_size", DEFAULT_MAX_MESSAGE))
        while chunk := f.read(sizer.size):
            if transfer.acked and not await self._window_open(transfer):
                return
            await self._channel.drain()
            # Either side may have cancelled while we waited for the buffer
            if transfer.status.finished:
                return
            self._channel.send(encode_chunk(transfer.id, offset, chunk))
            transfer._hash.update(chunk)
            offset += len(chunk)
            transfer.bytes_done = offset
            sizer.sent(len(chunk), time.monotonic())
            self._report(transfer)

        if offset != transfer.size:
            self._fail(transfer, "file changed while it was being sent")

    async def _window_open(self, transfer: Transfer) -> bool:
        """Wait until fewer than ACK_WINDOW bytes are unacknowledged.
        False if the transfer ended meanwhile or acks stopped coming."""
        while transfer.bytes_done - transfer.bytes_acked >= ACK_WINDOW:
            if transfer.status.finished:
                return False
            transfer._ack_event.clear()
            try:
                await asyncio.wait_for(transfer._ack_event.wait(), self._stall_timeout)
            except TimeoutError:
                # Nothing to tell the receiver (the path to it is what failed),
                # and it must keep its partial for the retry
                quiet = f"{self._stall_timeout:.0f}s"
                self._interrupt(transfer, f"stalled: nothing acknowledged for {quiet}")
                if self._on_stall is not None:
                    self._on_stall()
                return False
        return not transfer.status.finished

    # ── Receiving ───────────────────────────────────────────────────

    async def handle_message(self, msg: ProtocolMessage) -> bool:
        """Handle a transfer-related protocol message.

        Returns False if ``msg`` isn't one, so the caller can dispatch
        it elsewhere.
        """
        payload = msg.payload
        if isinstance(payload, FileMetadataPayload):
            self._start_receive(payload)
        elif isinstance(payload, TransferAckPayload):
            transfer = self._active(payload.transfer_id)
            if (
                transfer is not None
                and transfer.direction is Direction.SEND
                and transfer.bytes_acked <= payload.offset <= transfer.bytes_done
            ):
                transfer.bytes_acked = payload.offset
                transfer._ack_event.set()
                self._report(transfer)
        elif isinstance(payload, TransferAcceptPayload):
            transfer = self._active(payload.transfer_id)
            if (
                transfer is not None
                and transfer.direction is Direction.SEND
                and not transfer._accepted.done()
            ):
                transfer._accepted.set_result(payload.offset)
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
        if transfer._send_acks and transfer.bytes_done - transfer._last_ack_sent >= ACK_EVERY:
            self._ack(transfer)
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
        if meta.resume_key is not None and not is_resume_key(meta.resume_key):
            self._reject(meta.transfer_id, "invalid resume key")
            return

        # Resume under the sender's key, unless another active transfer is
        # already writing that partial (the same file sent twice at once)
        key = meta.resume_key
        in_use = {t._partial for t in self._transfers.values() if not t.status.finished}
        resumable = key is not None and key not in in_use
        partial = key if resumable and key is not None else transfer_id
        offset = self._partials.existing(partial, meta.size) if resumable else 0

        self._download_dir.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self._download_dir).free < meta.size - offset:
            self._reject(meta.transfer_id, "not enough disk space on receiver")
            return

        hasher = hashlib.sha256()
        if offset:
            try:
                self._partials.hash_prefix(partial, offset, hasher)
            except OSError:
                # Unreadable partial: start over rather than fail
                self._partials.discard(partial)
                hasher, offset = hashlib.sha256(), 0
        try:
            file = self._partials.open(partial, meta.size, offset)
        except OSError as exc:
            self._reject(meta.transfer_id, f"cannot create file: {exc}")
            return

        filename = safe_filename(meta.filename)
        transfer = Transfer(
            id=transfer_id,
            direction=Direction.RECEIVE,
            filename=filename,
            size=meta.size,
            path=unique_path(self._download_dir, filename),  # re-checked on completion
            bytes_done=offset,
            resumed_from=offset,
            _hash=hasher,
            _file=file,
            _partial=partial,
            _resumable=resumable,
            _send_acks=meta.ack,
            _last_ack_sent=offset,
        )

        self._transfers[transfer_id] = transfer
        if key is not None:
            # The sender offered resume, so it waits for our answer
            self._send(
                MessageType.TRANSFER_ACCEPT,
                TransferAcceptPayload(transfer_id=meta.transfer_id, offset=offset),
            )
        self._report(transfer, force=True)

    def _handle_complete(self, payload: TransferCompletePayload) -> None:
        transfer = self._active(payload.transfer_id)
        if transfer is None:
            return

        if transfer.direction is Direction.SEND:
            # The receiver's confirmation: verified and saved
            transfer.bytes_acked = transfer.size
            self._finish(transfer, TransferStatus.COMPLETED, notify=False)
            return

        if transfer.bytes_done != transfer.size:
            self._fail(transfer, f"incomplete: got {transfer.bytes_done} of {transfer.size} bytes")
            return
        if transfer._hash.hexdigest() != payload.sha256.lower():
            self._fail(transfer, "checksum mismatch")
            return

        assert transfer._file is not None and transfer._partial is not None
        transfer._file.close()
        transfer._file = None
        # The name may have been taken while we were receiving
        final = transfer.path
        if final.exists():
            final = unique_path(final.parent, final.name)
        try:
            self._partials.finish(transfer._partial, final)
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
        when the channel is gone. Resumable partials are kept."""
        for transfer in list(self._transfers.values()):
            if not transfer.status.finished:
                self._interrupt(transfer, reason)

    # ── Internals ───────────────────────────────────────────────────

    def _active(self, transfer_id: str) -> Transfer | None:
        with contextlib.suppress(ValueError):
            transfer_id = uuid.UUID(transfer_id).hex
        transfer = self._transfers.get(transfer_id)
        if transfer is None or transfer.status.finished:
            return None
        return transfer

    def _ack(self, transfer: Transfer) -> None:
        """Tell the sender how much is stored. Flush first: an ack is a
        promise that the bytes survive this process dying."""
        assert transfer._file is not None
        try:
            transfer._file.flush()
        except OSError as exc:
            self._fail(transfer, f"write error: {exc}")
            return
        transfer._last_ack_sent = transfer.bytes_done
        with contextlib.suppress(ConnectionError):
            self._send(
                MessageType.TRANSFER_ACK,
                TransferAckPayload(transfer_id=transfer.id, offset=transfer.bytes_done),
            )

    def _interrupt(self, transfer: Transfer, reason: str) -> None:
        """End a transfer because the connection failed: keep partial data
        and flag it for a retry."""
        if transfer.status.finished:
            return
        transfer.interrupted = True
        self._finish(transfer, TransferStatus.FAILED, reason, notify=False, keep_partial=True)

    def _fail(self, transfer: Transfer, reason: str) -> None:
        self._finish(transfer, TransferStatus.FAILED, reason, notify=True)

    def _finish(
        self,
        transfer: Transfer,
        status: TransferStatus,
        reason: str = "",
        *,
        notify: bool,
        keep_partial: bool = False,
    ) -> None:
        """End a transfer. A receiver's partial data is deleted unless
        ``keep_partial`` and it's resumable: cancels and integrity failures
        must not leave data behind to be resumed."""
        if transfer.status.finished:
            return
        transfer.status = status
        transfer.reason = reason

        if transfer._file is not None:
            transfer._file.close()
            transfer._file = None
            if transfer._partial is not None and not (keep_partial and transfer._resumable):
                self._partials.discard(transfer._partial)

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
