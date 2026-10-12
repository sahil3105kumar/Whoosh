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

---

## Open issues

### I1. One lost `hello` with the CLI creating and a browser joining
*Observed 2026-10-12, once in six runs; not reproduced since.*

The CLI (`whoosh send`) connected but never received the browser's
`hello` and gave up after 10s. The browser showed "Connected". Five
later runs of the same setup succeeded, and logs from them show
`hello` sent and received in order. The CLI now logs every DataChannel
message type with `-v`; capture that output and the browser console if
it happens again.

### I2. Browser → CLI transfers are slower than the reverse
*Observed 2026-10-12.*

Browser → CLI ran at ~7.5 MB/s, against CLI → browser at ~32 MB/s and
browser → browser at ~47 MB/s over localhost. The limit is on aiortc's
receiving side, not the web client. Fine for v1; worth profiling if
large uploads from the web are common.
