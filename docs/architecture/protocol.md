# Protocol

All peer communication occurs over a single reliable, ordered WebRTC
DataChannel. It carries two kinds of frames:

* **Text frames** — JSON protocol messages (this document).
* **Binary frames** — file data (see [File chunks](#file-chunks)).

The signaling server's WebSocket messages (`offer`, `answer`, `ice`,
`peer_joined`, `peer_disconnected`, `peer_left`, `error`) are a separate
protocol and never cross the DataChannel; see [Rooms and
signaling](#rooms-and-signaling).

Rooms hold up to 8 people as a **full mesh**: every pair of peers has its
own connection and DataChannel, and everything in this document happens
per pair. Room-wide actions (group chat, sending a file to everyone) are
the same message sent on each pair's channel.

## Envelope

Every text frame is one message in the same envelope.

```json
{
  "version": 1,
  "type": "message_type",
  "id": "uuid",
  "payload": {}
}
```

`payload` is validated against the model registered for `type`.

## Message Types

### hello

Sent by both peers as soon as the channel opens.
`{"capabilities": ["chat", "file_transfer", "cancel", "resume", "ack"], "name": "alice" | null}`

`name` is the display name shown to others; it's self-chosen, so it's a
label, not an identity. Receivers strip control characters and cap it at
32 characters.

Optional behaviour (like `resume`) is used only when *both* peers
advertise it, so clients with different feature sets still interoperate.

A peer must not start a file transfer until the other side's `hello`
has advertised `file_transfer`.

### chat

`{"text": "..."}`

### file_metadata

Announces an incoming file. Sender → receiver.
`{"transfer_id": "<uuid hex>", "filename": "...", "size": 123, "mime_type": "...", "resume_key": "<hex>" | null, "ack": false}`

`resume_key` is sent only when the receiver advertised `resume`; the
receiver must then answer with `transfer_accept`. It identifies the
file across sessions (see [Resume](#resume)). `ack: true` (sent only
when the receiver advertised `ack`) asks for `transfer_ack` messages.

`filename` is a suggestion only: the receiver strips any directory
components and picks a non-colliding name in its download directory.

### transfer_accept

`{"transfer_id": "...", "offset": 0}` — receiver → sender, in answer to
a `file_metadata` that carried a `resume_key`. `offset` is how many
bytes of the file the receiver already holds; the sender streams from
there. 0 means start from the beginning.

### transfer_complete

`{"transfer_id": "...", "sha256": "<hex digest of the whole file>"}`

Sent twice per successful transfer:

1. Sender → receiver after the last chunk.
2. Receiver → sender, echoing the payload, once the file's size and
   hash are verified and it has been saved under its final name.

The sender treats a transfer as complete only on (2).

### transfer_cancel

`{"transfer_id": "...", "reason": "..."}`

Ends a transfer for both sides. Either peer may send it at any time,
for a user cancel or for an error (checksum mismatch, disk full,
out-of-order chunk, rejected metadata). The receiver deletes any
partial data. Chunks still in flight after a cancel are dropped.

### transfer_ack

`{"transfer_id": "...", "offset": 1048576}` — receiver → sender, when
`file_metadata` asked for acks: every byte before `offset` has been
written to storage. Sent each time another 1 MiB is stored; the final
`transfer_complete` echo covers the tail.

The sender shows acknowledged bytes as its progress and keeps at most
8 MiB unacknowledged. A full window with no ack for 20 s fails the
transfer as stalled: the connection is dead even if it hasn't said so.
Acks outside `[last ack, bytes sent]` are ignored.

### error

`{"code": "...", "message": "..."}` — the peer sent something that
couldn't be decoded (`bad_message`, `bad_frame`). Never answered with
another error.

### bye

`{}` — sent just before leaving on purpose (quit, Leave, tab closed, a
one-shot send finished). The receiver ends the session. Without a `bye`,
a closed connection is treated as a network failure and both sides try
to reconnect (see [Reconnection](#reconnection)). Best-effort: a closing
browser tab may not get it out in time, in which case the other side
gives up after its reconnect window.

### ping / pong

`{"timestamp": <float>}` — `pong` echoes the `ping`'s timestamp, so the
sender measures round-trip time against its own clock only.

## File chunks

File data travels in binary frames with a fixed 25-byte header:

| Offset | Size | Field         | Notes                                   |
|--------|------|---------------|-----------------------------------------|
| 0      | 1    | `kind`        | `0x01` = file chunk; others reserved    |
| 1      | 16   | `transfer_id` | UUID bytes from `file_metadata`         |
| 17     | 8    | `offset`      | Byte offset in the file, big-endian     |
| 25     | …    | data          | Up to 16 KiB                            |

Chunks for a transfer arrive strictly in offset order (the channel is
ordered); a gap or overlap fails the transfer.

## Transfer sequence

```
sender                                   receiver
  │ file_metadata ─────────────────────────▶ │  create <name>.part
  │ chunk @0, chunk @16384, ... ───────────▶ │  append, hash
  │ transfer_complete (sha256) ────────────▶ │  verify size + hash, rename
  │ ◀──────────────────── transfer_complete  │
```

## Resume

An interrupted transfer continues when the same file is sent again, in
any later session or room.

**Resume key** — computed by the sender, identical in both clients:

```
sha256(
  utf8("whoosh-resume-v1\n{size}\n{mtime_ms}\n")
  || file[0 : min(1 MiB, size)]
  || file[max(0, size - 1 MiB) : size]
)  →  lowercase hex
```

`mtime_ms` is the modification time in whole milliseconds. A false match
can't corrupt anything: the full-file SHA-256 in `transfer_complete` is
still verified.

**Sequence** with a partial copy of 400 of 1000 bytes on the receiver:

```
sender                                   receiver
  │ file_metadata (resume_key) ────────────▶ │  finds partial: 400 bytes
  │ ◀──────────────── transfer_accept (400)  │
  │ chunk @400, ... ──────────────────────▶ │  append, hash (prefix re-hashed)
  │ transfer_complete (sha256 of all 1000) ▶ │  verify, rename
  │ ◀──────────────────── transfer_complete  │
```

The sender hashes the skipped prefix locally, so the final SHA-256 still
covers the whole file. Partial data is kept when the connection drops,
and deleted on `transfer_cancel` or any integrity failure.

## Rooms and signaling

* On connecting, the server sends `{"type": "hello", "peer_id": ..., "peers": [...]}`:
  our slot, plus the peers already connected. Peers that arrive later
  are announced with `peer_joined`; `peer_left` means a slot was released
  (left, or gone longer than the 30 s grace period).
* `offer` / `answer` / `ice` carry `"to": "<peer_id>"`; the server
  delivers them to that peer only, adding `"from": "<sender>"`. (Without
  `to` they go to everyone, which only makes sense in a 1:1 room.)
* **In each pair, the peer with the smaller `peer_id` makes the offer**,
  whenever that pair isn't connected and both are on signaling. The
  other side accepts any offer from that peer, replacing its old
  connection.

## Reconnection

When the peer-to-peer connection drops without a `bye` (lost network,
crash, or a transfer stalling with no acks), both sides keep their slot
in the room and reconnect:

* A dropped signaling WebSocket reconnects with `?peer_id=` to reclaim
  its slot.
* The offering side of the pair (smaller `peer_id`) sends a fresh offer
  whenever the pair is down and both peers are on signaling (on its own
  reconnect, on `peer_joined`, and shortly after a drop). The other side
  accepts any new offer, replacing its old connection.
* On the new connection both sides send `hello` again. Interrupted
  outgoing transfers are re-sent (up to 3 attempts per file) and resume
  from the receiver's partial copy.

## Versioning

Every protocol message includes a version number to allow backward-compatible evolution.

Future protocol versions may introduce new capabilities while maintaining compatibility with older clients.
