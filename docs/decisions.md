# Decisions

A running log of design decisions: what was chosen, why, and what would
make us revisit it. Newest at the bottom. When a decision is reversed,
add a new entry that supersedes the old one rather than editing history.

Format: **Decision** — what we do. **Why** — the reasoning.
**Revisit** — the condition under which this should be reconsidered.

---

## Signaling server

### D1. Peer slots survive a disconnect for a 30s grace period
*2026-10-12*

**Decision** — When a peer's WebSocket drops, it keeps its room slot
for 30s (`WHOOSH_RECONNECT_GRACE_SECONDS`). Reconnecting with its
`peer_id` within that window reclaims the slot; otherwise it's released
and the other peer receives `peer_left`.
**Why** — Holding the slot until room expiry (up to 10 min) left rooms
"full" after a crash; releasing immediately breaks reconnection across
a network blip.
**Revisit** — If real users reconnect after longer gaps (e.g. mobile).

### D2. Socket cleanup is identity-checked and unconditional
*2026-10-12*

**Decision** — The WebSocket handler always unregisters in a `finally`,
and `disconnect()` only removes the entry if it still points at *that*
socket. A failed send during relay is logged and skipped.
**Why** — A bad frame used to skip cleanup, leaking the slot; and a
stale socket's cleanup could unregister a peer that had already
reconnected on a new socket.

### D3. Expired rooms are swept every 60s; TTL counts from creation
*2026-10-12*

**Decision** — Lookup-time expiry stays, plus a background sweep every
60s for rooms nobody looks up again. A room's TTL (10 min,
`WHOOSH_ROOM_TTL_SECONDS`) runs from creation, not from last activity.
**Why** — Without the sweep, abandoned rooms lived in memory forever.
Expiry only blocks *new* joins and reconnects; an established P2P
session doesn't need signaling, so it is unaffected.
**Revisit** — If peers need to reconnect to signaling late in a long
session (e.g. for ICE restarts), extend `expires_at` on activity.

### D4. The relay accepts only `offer`, `answer`, `ice`
*2026-10-12*

**Decision** — Peers may relay only these types. Anything else gets an
`error` reply and the connection stays open. The server still never
looks inside SDP or candidates.
**Why** — Stops a peer from spoofing server events (`peer_joined`,
`peer_left`) to the other side.

### D5. Framing limits on the signaling socket
*2026-10-12*

**Decision** — Text frames holding a JSON object, at most 64 KiB
(`WHOOSH_MAX_MESSAGE_BYTES`). Violations close the socket: binary
→ 1003, invalid/non-object JSON → 1007, too big → 1009.
**Why** — SDP is a few KB; anything far larger is a bug or abuse.
Uvicorn's `--ws-max-size` should also be set in deployment so oversized
frames are refused before they're buffered.

### D6. Origin allowlist; requests without `Origin` always pass
*2026-10-12*

**Decision** — `WHOOSH_ALLOWED_ORIGINS` (comma-separated) restricts
browser origins on the WebSocket and enables CORS for them. Empty means
unrestricted (local dev). Requests with no `Origin` header (CLI, curl)
are always allowed.
**Why** — The check exists to stop other websites driving the server
from a visitor's browser. Non-browser clients can forge any header, so
gating them on `Origin` would add nothing.

### D7. Per-IP token bucket on room creation, in memory
*2026-10-12*

**Decision** — `POST /rooms` is limited to 10 per minute per client IP
(`WHOOSH_ROOM_CREATE_PER_MINUTE`), returning 429. In-process token
bucket; idle buckets pruned by the sweep.
**Why** — Room IDs are short (8 hex chars) on purpose; capping creation
keeps both memory and the ID space healthy. No Redis needed for a
single instance.
**Revisit** — Behind a reverse proxy, uvicorn must run with
`--proxy-headers` or every client shares the proxy's IP. Multi-instance
deployment needs a shared store (see D9).

### D8. Configuration via `WHOOSH_*` env vars and a plain dataclass
*2026-10-12*

**Decision** — `apps/server/config.py` reads environment variables into
a frozen dataclass. No pydantic-settings.
**Why** — A handful of scalars doesn't justify another dependency.

### D9. Single process, in-memory state
*2026-10-12 (pre-existing, recorded here)*

**Decision** — Rooms, connections and rate-limit buckets live in one
process. Deploy with exactly one uvicorn worker.
**Why** — Signaling load is tiny; shared state would mean Redis and
pub/sub for no v1 benefit.
**Revisit** — If a single instance can't keep up or high availability
becomes a requirement.

---

## NAT traversal

### D10. ICE servers come from the server's `GET /config`
*2026-10-12*

**Decision** — Clients fetch ICE servers from `/config` (shape matches
the browser's `RTCIceServer`) instead of hardcoding them. Default:
Google's public STUN server (`WHOOSH_STUN_URLS`).
**Why** — Adding TURN later becomes a server config change with no
client release.

### D11. No TURN in v1; fail with a clear message instead
*2026-10-12*

**Decision** — TURN stays deferred to v4.0. When ICE fails, clients say
so explicitly ("no network path between the peers… strict NAT or
firewall") rather than reporting a generic timeout.
**Why** — Running TURN is real operational cost (bandwidth, auth). An
estimated 10–20% of peer pairs, those behind symmetric NATs, won't connect.
**Revisit** — If failure reports from real users are common.

---

## Protocol

### D12. File data travels as binary frames, not base64 JSON
*2026-10-12*

**Decision** — Text frames carry JSON messages; binary frames carry
file chunks with a 25-byte header (kind, 16-byte transfer UUID, 8-byte
offset). `file_chunk` is no longer a JSON message type. Spec in
`docs/architecture/protocol.md`.
**Why** — Base64 adds ~33% to every byte and costs CPU to encode and
decode on both ends.
**Details** — `offset` rather than chunk index so v2.0 resume can
restart from any byte without agreeing on chunk size. Chunks are
16 KiB, under every DataChannel implementation's safe message size.
Transfer IDs are UUIDs so they pack into 16 bytes.

### D13. `peer_join`/`peer_leave` removed from the DataChannel protocol
*2026-10-12*

**Decision** — Removed.
**Why** — Presence is a signaling concern and the server already sends
`peer_joined`/`peer_left`. Over the DataChannel they could only ever
describe the one peer you're already talking to.

### D14. Protocol version stays 1 despite the changes
*2026-10-12*

**Decision** — D12 and D13 change the wire format without bumping
`PROTOCOL_VERSION`.
**Why** — No client has been released, so there's nothing to stay
compatible with. From the first release on, wire changes bump the
version.

### D15. The codec validates payloads against the model for their `type`
*2026-10-12*

**Decision** — `decode()` validates `payload` with the model registered
for the message's `type`, not the payload union.
**Why** — The union is undiscriminated and several payloads share a
shape. `transfer_cancel` was decoding as `TransferCompletePayload` and
`pong` as `PingPayload`.

### D16. `transfer_cancel` ends transfers; `error` is for undecodable input
*2026-10-12*

**Decision** — Every transfer-ending event, whether user cancel, checksum
mismatch, disk full or bad chunk, is a `transfer_cancel` with a reason.
`error` is only for messages or frames that couldn't be decoded, and is
never answered with another `error`.
**Why** — One path for "this transfer is over" keeps both state
machines simple; the no-error-for-error rule prevents loops.

---

## File transfer

### D17. SHA-256 verification, confirmed by the receiver
*2026-10-12*

**Decision** — The sender includes the file's SHA-256 in
`transfer_complete`. The receiver verifies size and hash, saves, and
echoes `transfer_complete`. The sender reports success only on that
echo, and fails after 60s without one.
**Why** — A reliable channel guarantees delivery of what was sent, not
that the file was saved intact on the other side. Pulls a slice of
v2.0's integrity work forward because it's cheap.

### D18. Progress is computed locally; `progress` stays reserved
*2026-10-12*

**Decision** — Each side shows progress from its own byte counts. The
receiver does not send `progress` messages in v1.
**Why** — The sender's view lags the receiver by at most the 1 MiB send
buffer (D21), so acknowledgements would add traffic without changing
what the user sees. v2.0 chunk acks/resume will give it a real job.

### D19. Received files: `.part` → rename, sanitized name, never overwrite
*2026-10-12*

**Decision** — Incoming data is written to `<name>.part` and renamed
only after verification; on any failure the partial file is deleted.
The peer's filename is reduced to a bare name (no directories, no
separators or control or Windows-reserved characters, ≤255 bytes), and a
clash becomes `name (1).ext`. Free disk space is checked against the
announced size before accepting.
**Why** — A peer must never decide *where* a file lands or clobber an
existing file. Half-written files must not look complete.

### D20. Incoming files are auto-accepted in v1
*2026-10-12*

**Decision** — No accept/reject prompt; files save to the download
directory (default: current directory, `--out` to change).
**Why** — Rooms are 1:1 and you joined a code someone gave you, so the
peer is someone you chose to connect to. A prompt is UI work better
done once there's a web client.
**Revisit** — Before multi-peer rooms (v3.0), or if a size limit or
confirmation is wanted.

### D21. Backpressure: 1 MiB high water, 256 KiB low water
*2026-10-12*

**Decision** — The sender awaits `drain()` before each chunk; it blocks
while more than 1 MiB is buffered and resumes at 256 KiB (via
`bufferedAmountLowThreshold`).
**Why** — Without it, a large file is queued into memory as fast as it
can be read and the channel stalls.

### D22. The transfer package doesn't know about aiortc
*2026-10-12*

**Decision** — `TransferManager` talks to any object with `send()` and
`drain()`. `WebRTCPeer` provides them.
**Why** — Follows the dependency rules in system_design.md, and lets
transfer logic be tested over an in-memory channel.

### D23. Disk I/O runs synchronously on the event loop
*2026-10-12*

**Decision** — 16 KiB reads and writes happen inline, not in a thread.
**Why** — Local disk ops of that size take microseconds; thread hops
per chunk would cost more than they save. Measured ~33 MB/s end to end
over localhost, bounded by aiortc's SCTP, not disk.
**Revisit** — If transfers to slow or network filesystems stall chat.

---

## RTC layer

### D24. Incoming DataChannel messages go through an ordered queue
*2026-10-12*

**Decision** — `WebRTCPeer.recv()` returns messages from a FIFO queue
(None on close), replacing the `on_message` callback that spawned a
task per message.
**Why** — A task per message dropped exceptions silently and, as soon as
a handler awaited anything, could process messages **out of order**,
which for file chunks means corruption.

---

## CLI

### D25. Failures in the signaling listener surface immediately
*2026-10-12*

**Decision** — The CLI races "connection ready" against the signaling
listener task and re-raises the listener's exception.
**Why** — Errors (e.g. a bad SDP) used to vanish with the task and show
up only as a 10s timeout.

### D26. Wait indefinitely for a peer, then 30s to connect
*2026-10-12*

**Decision** — No timeout while waiting for the other person to join;
once negotiation starts, 30s for the DataChannel to open.
**Why** — The old single 10s timeout made `create` give up if the other
person took more than 10s to type the room code. 30s covers STUN
gathering on machines with many interfaces (~11s observed on a dev box
with Docker/libvirt bridges).

### D27. Keep argparse rather than Typer
*2026-10-12*

**Decision** — The CLI uses argparse; the README's Typer mention is
updated.
**Why** — Four subcommands don't need another dependency. Typer is
still an easy swap if richer help output is wanted.

### D28. Two interactive commands, two one-shot commands
*2026-10-12*

**Decision** — `create`/`join` open a chat with `/send`, `/cancel`,
`/ping`, `/quit`; `send FILE…`/`receive ROOM` transfer and exit (exit
code 1 if anything failed). stdin is read by a daemon thread.
**Why** — One-shot commands are scriptable (`whoosh send` like
`scp`); interactive mode is the chat product. A daemon thread is
portable (unlike `connect_read_pipe` on Windows) and doesn't block exit.

### D29. Packaged with hatchling; `WHOOSH_SERVER` sets the default server
*2026-10-12*

**Decision** — A `whoosh` console script, built with hatchling, ships
the top-level `apps` and `packages` directories. `--server` defaults to
`$WHOOSH_SERVER`, else `http://localhost:8000`.
**Why** — Enables `uv tool install` / `pipx`, which put the tool in an
isolated environment, so the generic top-level names can't clash.
**Revisit** — Before publishing to PyPI: move under a `whoosh`
namespace package and make the public server the default.
*Update: the CLI now installs as `whoosh[cli]`; see D31.*

---

## Deployment

### D30. Uvicorn trusts forwarded headers from any source; the app port is never published
*2026-10-12*

**Decision** — The container runs uvicorn with `--proxy-headers
--forwarded-allow-ips '*'`. In `docker/compose.yaml` the app port is
only `expose`d on the internal network, never mapped to the host.
**Why** — Per-IP rate limiting (D7) needs the real client IP. Caddy
replaces any client-supplied `X-Forwarded-For` with the actual remote
address, so trusting it is safe *as long as* Caddy is the only path to
the app. Pinning Caddy's container IP instead would break whenever the
network is recreated.
**Revisit** — If the app is ever exposed directly, set
`--forwarded-allow-ips` to the proxy's address. Otherwise anyone can
spoof their IP and bypass the rate limit. (Verified locally: through
Caddy the app logs the client's address, not Caddy's.)

### D31. Dependencies split into `cli` and `server` extras
*2026-10-12*

**Decision** — Base dependencies are just `pydantic` (the shared
protocol models). `whoosh[cli]` adds aiortc and websockets;
`whoosh[server]` adds fastapi, uvicorn and websockets. The dev group
installs both. Installing the CLI without its extra prints the correct
install command instead of a traceback.
**Why** — The WebRTC stack (`av`, `cryptography`, `pylibsrtp`) was 125
of the server image's 150 MB of Python packages and is never imported
by the server. Image 270 MB → 135 MB, with a smaller attack surface.
**Gotcha** — uvicorn answers WebSocket upgrades with **404** unless a
WebSocket library is installed. When websockets left the base
dependencies, signaling broke in the image while every in-process test
still passed. Caught by D33's smoke test; `server` now lists websockets
explicitly.

### D32. Caddy as the reverse proxy
*2026-10-12*

**Decision** — Caddy 2 in front of the app, configured by a
four-line Caddyfile.
**Why** — Automatic HTTPS (issue and renew) with no certbot cron, and
WebSocket proxying with no extra config. HTTPS isn't optional: browsers
only allow WebRTC on secure origins, which the web client will need.

### D33. CI smoke-tests the built image over real sockets
*2026-10-12*

**Decision** — CI builds the Docker image, waits for its health check,
and runs `scripts/smoke_test.py` against it: HTTP endpoints, two
WebSocket peers, and a relayed message. The same script verifies a live
deployment.
**Why** — In-process tests use the full dev environment and an ASGI
transport, so they can't see packaging or proxy problems (see D31's
gotcha).

### D34. Target host: one Docker VPS, provider-agnostic
*2026-10-12*

**Decision** — `docs/deployment.md` targets any Linux VPS running
Docker Compose. No platform-specific config (Fly.io, etc.).
**Why** — Single process with in-memory state (D9) wants one
long-running machine, not autoscaling. Compose plus Caddy runs the same
way locally (as tested) and on any provider.

---

## Web client

### D35. No trickle ICE: SDP is sent once candidate gathering completes
*2026-10-12*

**Decision** — The browser waits for ICE gathering to complete (capped
at 5s) and sends one offer or answer with all candidates inline. No
`ice` messages are sent.
**Why** — aiortc neither trickles nor accepts trickled candidates, and
the CLI ignores `ice`. A browser that trickled would fail to connect to
the CLI. Gathering first costs a few seconds of setup and works with
every client.
**Revisit** — If connection setup time matters enough to add trickle
support to the CLI (aiortc's `addIceCandidate`).

### D36. TypeScript protocol types are generated by our own script
*2026-10-12*

**Decision** — `scripts/gen_ts_protocol.py` writes
`apps/web/src/lib/protocol.gen.ts` from the Pydantic models: payload
interfaces, enums, a `MessageMap` (type → payload) and `PAYLOAD_FIELDS`,
a runtime field spec the web decoder validates against. CI fails if the
file is stale (`--check`).
**Why** — json-schema-to-typescript (the original plan) produces the
payload interfaces but not the type → payload map or a runtime
validator, and those are what make the envelope type-safe and incoming
messages trustworthy. The payloads are simple enough (strings, numbers,
string arrays) that a 100-line generator covers them.
**Revisit** — If payloads grow nested or union-typed fields.

### D37. Incremental SHA-256 via @noble/hashes
*2026-10-12*

**Decision** — The web client hashes with `@noble/hashes`.
**Why** — WebCrypto's `digest()` only hashes a whole buffer, which would
mean holding entire files in memory to hash them. noble is small,
audited, dependency-free, and fast enough: a 20 MB transfer ran at
~47 MB/s browser to browser with hashing on both sides.

### D38. Received files are held as Blobs and saved with a link
*2026-10-12*

**Decision** — Incoming chunks are folded into a `Blob` every 8 MiB;
on verification the parts become one Blob behind an object URL with a
**Save** link. No File System Access API, and no free-space check, in
v1.
**Why** — Works in every browser, and browsers can page large Blobs to
disk. Writing straight to disk via File System Access is Chromium-only
and needs a user gesture before data arrives, which means an
accept-file flow (see D20).
**Revisit** — For multi-GB files, or together with an accept/reject
prompt.

### D39. The Python server serves the built web client
*2026-10-12*

**Decision** — With `WHOOSH_WEB_DIR` set (the Docker image sets it),
FastAPI serves the static build at `/`, mounted after all API routes.
The image gains a Node build stage.
**Why** — One container and one origin: no CORS, and the default origin
allowlist (`https://$WHOOSH_DOMAIN`) just works. The files are about
80 KB gzipped, so serving them from Python costs nothing worth
optimizing.
**Revisit** — If the web app needs a CDN, let Caddy serve the files
directly.

### D40. Invite links are `/#<room id>`; the creator role isn't persisted
*2026-10-12*

**Decision** — Opening `https://host/#abcd1234` joins that room. The
creator's page doesn't put the code in its own URL.
**Why** — A hash never reaches the server, so links need no routing.
If the creator's URL held the code, a refresh would rejoin as a second
peer while the creator's old slot is still in its grace period, and
the room would be full.

### D41. Web client unit tests use Node; interop was verified by hand
*2026-10-12*

**Decision** — Vitest covers frames (byte-identical to the Python
encoder), protocol validation, filenames and the transfer manager over
an in-memory channel. Browser↔CLI and browser↔browser transfers were
verified manually in a real browser (Chromium); there is no automated
browser end-to-end test yet.
**Why** — Node has no WebRTC, and a Playwright-plus-aiortc harness in CI
is a project of its own.
**Revisit** — Before the next protocol change: an automated interop
test is the only thing that would catch a regression like D24 or D15
across the two implementations.
*Superseded by D42.*

### D42. Automated browser ↔ CLI interop tests with Playwright
*2026-10-13 — supersedes D41's manual verification*

**Decision** — `tests/interop/` runs a real server, the built web client
in headless Chromium (Python Playwright) and the CLI as a subprocess:
browser → CLI and CLI → browser transfers (the latter 5× per run, for
I1), chat and ping both ways, and browser ↔ browser. Files are checked
byte for byte. Playwright lives in a separate `interop` dependency
group with its own CI job; without it (or without a web build) the
tests skip, so a plain `uv run pytest` needs no browser.
**Why** — The only check that the Python and TypeScript implementations
still agree. ~20s per run. STUN is disabled in the test server: the
peers share a machine, and gathering is slow on hosts with many
interfaces.
**Gotcha** — The first version read the CLI's stdout only when waiting
for a line. With `-v` the receiving CLI logs every SCTP chunk, the
64 KB pipe filled, and the CLI's event loop blocked mid-transfer. That
looked exactly like an app bug (upload stuck at 0%). The harness now
drains output continuously.

---

## Open issues

### I1. One lost `hello` with the CLI creating and a browser joining
*Observed 2026-10-12, once in six manual runs. Not reproduced in 25
further runs: 5 manual and 20 automated (D42).*

The CLI (`whoosh send`) connected but never received the browser's
`hello` and gave up after 10s. The browser showed "Connected". Five
later runs of the same setup succeeded, and logs from them show
`hello` sent and received in order. The CLI now logs every DataChannel
message type with `-v`; capture that output and the browser console if
it happens again. The interop suite repeats this scenario on every CI
run, so a recurrence there would come with full CLI output.

### I2. Browser → CLI transfers are slower than the reverse
*Observed 2026-10-12.*

Browser → CLI ran at ~7.5 MB/s, against CLI → browser at ~32 MB/s and
browser → browser at ~47 MB/s over localhost. The limit is on aiortc's
receiving side, not the web client. Fine for v1; worth profiling if
large uploads from the web are common.

---

## v1.1 — Usability

### D43. Outgoing files queue and go one at a time
*2026-10-19 — #14*

**Decision** — Both clients keep an outgoing queue (`apps/web/src/lib/queue.ts`,
`apps/cli/queue.py`): files are sent in the order added, one at a time, and
waiting ones can be removed (web: **Remove**; CLI: `/queue`, `/cancel`). The
queue is client-side only; the protocol is unchanged.
**Why** — Previously each drop or `/send` started its own concurrent upload.
Concurrent sends only split one DataChannel's bandwidth, so every file finished
later than it would in sequence, and the progress of several half-done files is
harder to follow than one active file plus a list.
**Revisit** — If many tiny files need batching: per-file round trips
(metadata → confirm) will dominate.

### D44. The whole room page is a drop target
*2026-10-19 — #13*

**Decision** — While connected, files dropped anywhere on the page are queued,
with a full-page "Drop to send" overlay during the drag. The drop box stays as a
visual hint and keeps the file picker.
**Why** — A small target is easy to miss, and a missed drop makes the browser
open the file and leave the room.

### D45. CLI output: colour only on terminals; invite links work as room arguments
*2026-10-19 — #15*

**Decision** — Colour and the redrawn progress bar (with speed and ETA) are used
only when stdout is a terminal and `NO_COLOR` is unset; piped output stays plain
text. `join`/`receive` accept an invite link as well as a code, and the link's
server is used unless `--server` is given (precedence: `--server`, link,
`$WHOOSH_SERVER`, default). Server close reasons are translated into advice
("Room x doesn't exist… rooms expire after 10 minutes").
**Why** — Scripts and logs shouldn't get escape codes. Pasting the link someone
sent you should just work, without knowing which server it came from.

---

## v2.0 — Reliable transfers

### D46. Resume: sampled resume key, `transfer_accept`, negotiated capability
*2026-10-19 — #17*

**Decision** — When both peers advertise `resume`, `file_metadata`
carries a `resume_key`: SHA-256 over the size, modification time, and
first and last 1 MiB of the file (spec in protocol.md, with shared
reference vectors in both test suites). The receiver answers
`transfer_accept {offset}` with the bytes it already holds; the sender
hashes that prefix locally and streams the rest. The protocol version
stays 1.
**Why** —
- *Not a full-file hash:* that would read huge files twice before
  sending a byte. A false match is harmless because the end-to-end
  SHA-256 (D17) still has to pass.
- *Byte offset:* chunk frames already carry byte offsets, chosen for
  exactly this (D12).
- *Capability, not version:* the new fields are additive and only used
  when both sides opt in, so mixed clients keep working.
- *Diverges from #17's wording* ("last acked chunk index", a
  `transfer_resume` message): the receiver's own byte count is the
  ground truth of what landed, so it needs no ack bookkeeping to resume.
**Revisit** — If files are often modified in place between attempts and
users expect resume anyway (the key changes with mtime).

### D47. Where partials live, and when they're kept
*2026-10-19 — #17*

**Decision** —
- *CLI:* partials are stored on disk in
  `<download dir>/.whoosh-partial/<key>.part` with a `.json` sidecar
  recording the size. They survive restarts, untouched ones are deleted
  after 7 days, and the folder disappears when empty.
- *Web:* partials stay in memory for the life of the tab, with the hash
  state kept so resuming doesn't re-read data.
- *Keep vs. discard:* a partial is kept when the connection drops, and
  deleted on cancel (by either side) or any integrity failure
  (checksum, offset, overflow). Transfers from senders without resume
  never keep partials.

**Why** — A cancel is a decision not to have the file; integrity failures
mean the partial can't be trusted. Web persistence across reloads would
need OPFS or IndexedDB and a quota story; in-tab covers the common
case of a peer's network dropping.
**Revisit** — Web persistence across reloads (OPFS).

### D48. A peer that stops responding counts as gone
*2026-10-19 — found while testing #17*

**Decision** — Both clients treat the connection as closed when ICE
reports `failed`; the web client also after `disconnected` for 8s. A
single "closed" latch in each client feeds the session, whichever
signal fires first.
**Why** — A peer that crashes or loses its network never closes the
DataChannel, so its "close" event may never arrive: the interop test
that kills the CLI mid-transfer left the browser waiting indefinitely.
ICE consent checks are the only signal. 8s rides out brief blips,
which ICE recovers from by itself.

### D49. Acknowledgements: `transfer_ack` every MiB, 8 MiB window, 20 s stall limit
*2026-10-19 — #18*

**Decision** — When both peers advertise `ack`, the receiver sends
`transfer_ack {offset}` each time another 1 MiB is written (the CLI
flushes to disk first: an ack is a promise the bytes are stored). The
sender's progress counts acknowledged bytes, at most 8 MiB may be
unacknowledged, and a full window with no ack for 20 s fails the
transfer as stalled. `transfer_ack` replaces the reserved, never-used
`progress` message.
**Why** —
- *Progress:* "sent" only means handed to the local SCTP stack.
  Acknowledged bytes are what actually landed, which is what #18 asks for.
- *Window:* bounds memory on a receiver that can't keep up, and turns
  "no acks" into a reliable stall signal (#19).
- *Granularity:* per MiB rather than per chunk; one ack per 64 chunks
  costs almost nothing.
- *Protocol change:* `progress` was never sent by anyone, so replacing
  it costs nothing before a release (D14).

### D50. Retry means reconnect and resume; `bye` marks a deliberate exit
*2026-10-19 — #19*

**Decision** — Both clients survive a dropped connection: the signaling
socket reconnects with its `peer_id`, the room creator re-offers, the
joiner accepts any new offer, and the session moves onto the new
connection. Interrupted outgoing transfers are retried automatically
(3 attempts per file); with resume (D46) only the missing part is sent
again. A stalled transfer (D49) tears the dead connection down to
trigger this. A new `bye` message, sent on quit / Leave / tab close /
end of a one-shot send, tells the other side not to bother reconnecting.
**Why** — #19 asks to retry failed *chunks*. On a reliable, ordered
DataChannel individual chunks never fail (SCTP retransmits them); what
fails is the whole connection. Retrying at that level, with resume,
recovers from the failures that actually happen without a second
retransmission layer on top of SCTP.
**Details** — Without `bye`, an intentional exit looks exactly like a
crash, and the other side would spend a minute "reconnecting". `bye`
from a closing browser tab is best-effort (sent on `pagehide`); when it
doesn't make it, the other side gives up after the 60 s reconnect window.
Verified by interop tests that cut the browser's connection mid-transfer
in both directions.

### D51. Occupied rooms don't expire
*2026-10-19 — #19, revises D3*

**Decision** — The periodic sweep extends the expiry of every room with
a connected WebSocket by a full TTL. Only rooms nobody is connected to
expire.
**Why** — D3's expiry counted from creation, so a session older than 10
minutes couldn't reconnect after a drop: the room would be gone.

### D52. Adaptive chunk sizing: hill-climb on throughput, capped by the connection
*2026-10-19 — #21*

**Decision** — Senders start at 16 KiB chunks and double the size while
throughput, measured over ≥250 ms / ≥512 KiB windows, keeps improving by
5%; they halve it if a window is 20% slower. Chunks are capped at
256 KiB and at the connection's maximum message size: the browser reads
`pc.sctp.maxMessageSize` (64 KiB with aiortc, 256 KiB Chrome↔Chrome);
the CLI always assumes 64 KiB, aiortc's own limit. No protocol change:
frames carry byte offsets, so receivers accept any size.
**Measured** — 100 MB over localhost, each case run twice, against fixed
16 KiB chunks: browser → CLI 13.2 → 14.0 MB/s, CLI → browser 20.8 →
21.0 MB/s, browser → browser 35.1 → 35.1 MB/s. **No significant
difference here**: the bottleneck is aiortc's Python SCTP stack (it
splits every message into ~1200-byte fragments regardless), not message
count. A first single-run comparison suggested a 26% regression for
CLI → browser; repeating it showed that was noise.
**Why keep it** — #21 asks for it; it's small, bounded and tested on
both sides, and it can only help where per-message overhead dominates
(high-bandwidth links between browsers). It must not be described as a
measured speed-up.
**Revisit** — The sender measures how fast data is *queued*, which only
tracks delivery once the 8 MiB ack window (D49) is full; driving it from
acknowledged bytes would be a better signal.

### D53. Integrity verification is end-to-end SHA-256 (already in place)
*2026-10-19 — #20*

**Decision** — #20 is satisfied by D17: the sender's SHA-256 of the whole
file travels in `transfer_complete` and the receiver verifies size and
hash before the file appears under its final name. With resume (D46)
the receiver re-hashes its stored prefix and the sender hashes the part
it skipped, so the check still covers every byte. A mismatch discards
the partial so a retry starts clean.
**Why not in `file_metadata`, as #20 suggests** — putting the checksum
up front would mean reading the whole file before sending its first
byte (minutes, for a large file). At the end it's computed while
streaming, for free. Per-chunk checksums would duplicate what DTLS
(authenticated encryption) and SCTP (CRC32c) already guarantee in
transit.

### D54. Transfer history: local only, metadata only, easy to turn off
*2026-10-19 — #22*

**Decision** — Each client records finished transfers on its own
machine: the CLI in `$XDG_STATE_HOME/whoosh/history.jsonl` (`whoosh
history`, `--clear`; `WHOOSH_HISTORY_FILE` moves it, `WHOOSH_NO_HISTORY=1`
disables it; trimmed to 1000 entries), the web client in `localStorage`
("Recent transfers" on the home page with **Clear**; 200 entries).
Entries hold time, direction, name, size, outcome and, in the CLI, path
and verified SHA-256. Received file contents are never kept by history.
**Why** — #22 requires the server to stay stateless about user data.
JSON Lines appends cheaply and survives a crash mid-write (a torn last
line is skipped on read). History is a convenience, so failures to
write it (full disk, blocked storage) are silent.
**Note** — The test suite sets `WHOOSH_NO_HISTORY` for everything except
history's own tests; otherwise the interop tests' CLI processes would
write into the developer's real history.

---

## v3.0 — Multi-peer rooms

### D55. Full mesh, up to 8 peers; the smaller peer_id offers
*2026-10-19 — #23*

**Decision** — Every pair of peers in a room has its own WebRTC
connection. Rooms hold up to 8 people (`WHOOSH_MAX_PEERS`). Signaling
messages are addressed (`to`, server-stamped `from`). In each pair the
peer with the lexicographically smaller `peer_id` makes the offers; the
other accepts any offer from it. Clients keep one protocol `Session` per
peer, so resume, acks, retries and history work per pair unchanged.
**Why** —
- *Mesh, not a relay:* a relay (SFU-style) through the server would
  break "the server only introduces you" and turn it into a bandwidth
  bill. A mesh keeps all data peer-to-peer.
- *The cap:* the cost grows with pairs (n·(n−1)/2) and each sender
  uploads once per recipient. 8 keeps that sane for files and chat.
- *Offer rule:* creator/joiner roles don't generalise to N peers, and
  "who arrived first" is ambiguous after reconnects. Comparing ids is
  deterministic, needs no coordination, and survives reconnects.
**Revisit** — Larger rooms or video would need an SFU.

### D56. Group chat and broadcast fan out over the pairs; membership comes from signaling
*2026-10-19 — #24, #25, #26*

**Decision** — "Broadcast" (#25) is a room-level helper that sends the
same message on every connected pair's channel; group chat (#24) and
room-wide ping are built on it. Membership (#26) comes from the
signaling server (`hello.peers`, `peer_joined`, `peer_left`) plus the
P2P connection state, and `hello` gains an optional display `name`. The
UI lists who's here (connected / reconnecting) and announces joins and
departures.
**Why** — There's no shared channel in a mesh, so a broadcast *is* a
fan-out. #26 mentions `PEER_JOIN` / `PEER_LEAVE` protocol messages; those
were removed in D13, since presence is something only the signaling
server sees for everyone. Names are self-chosen labels, not identities:
proving who someone is belongs to v4.0 (device verification).

### D57. "Distribute a file to the room" = one transfer per recipient, in parallel
*2026-10-19 — #27*

**Decision** — Sending a file in a multi-peer room sends it to every
connected peer at once, each as an ordinary transfer with its own
progress, resume and retries. The CLI's `send --peers N` waits for N
people first. Receivers judge each file by its last attempt.
**Why** — It reuses everything v2.0 built per pair. A swarm (recipients
re-sharing chunks with each other, BitTorrent-style) would cut the
sender's upload, but needs chunk maps, peer selection and partial-file
serving: a large protocol for rooms of at most 8.
**Revisit** — If the sender's upload becomes the bottleneck in practice.

---

## v4.0 — Security

### D58. Room passwords: scrypt hash in memory, sent as the first WebSocket message
*2026-10-19 — #28*

**Decision** — `POST /rooms` takes an optional `{"password": ...}`; the
server keeps only a salted scrypt hash (16-byte salt, N=2^14) for the
life of the room. On a protected room, a WebSocket must send
`{"type": "auth", "password": ...}` as its first message (within 10 s)
before it gets a slot — on every reconnect too. Wrong passwords are
limited to 5 per minute per room and client address.
`GET /rooms/{id}` reports `password_required` so clients can ask first.
CLI: `--password [VALUE]` (bare: prompted, twice when creating) or
`$WHOOSH_PASSWORD`; web: an optional password field when creating, a
prompt when joining a protected room.
**Why** — Never in the URL: query strings end up in proxy and server
logs. The password gates *signaling* only — it keeps strangers who learn
the code out of the room. It isn't an encryption key; confidentiality
against the server itself comes from D59–D61.
**Note** — The invite link deliberately doesn't carry the password:
share it separately.

### D59. TURN relay with short-lived credentials; optional coturn in the stack
*2026-10-19 — #31*

**Decision** — With `WHOOSH_TURN_URLS` and `WHOOSH_TURN_SECRET` set,
`GET /config` adds a TURN server with credentials minted per request:
`username = "<expiry>:<random>"`, `credential = base64(HMAC-SHA1(secret,
username))`, valid for an hour (the "TURN REST API" scheme coturn
implements with `use-auth-secret`). `docker/compose.yaml` gains an opt-in
coturn service (`--profile turn`) that refuses to start without a secret
and never relays into private or loopback address ranges.
`WHOOSH_ICE_TRANSPORT_POLICY=relay` makes browsers use only the relay,
hiding peers' IP addresses from each other.
**Why** — Peers behind symmetric NATs can't connect directly (D11), and
long-lived TURN passwords in clients would be scraped and abused. With
expiring credentials, the secret stays between the signaling server and
the TURN server. Relayed data is still DTLS-encrypted end to end (and,
with D60, encrypted again at the application layer), so the relay sees
only ciphertext.
**Limits** — aiortc can't be told to use relay only, so the CLI ignores
`relay` policy (it still uses TURN when needed). The open-relay abuse
guard (`denied-peer-ip`) matters: without it, anyone with fresh
credentials could relay into the server's own network.
**Verified** — Against a real coturn: two relay-only browsers, and the
CLI to a relay-only browser, connect with the selected candidate pair of
type `relay`, and transfer files intact.
