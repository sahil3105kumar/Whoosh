# Roadmap

## v1.0 — Minimum Viable Product

* Room creation
* Room joining
* WebSocket signaling
* SDP negotiation
* ICE candidate exchange
* WebRTC DataChannel
* Peer-to-peer chat
* Streaming large file transfer
* Progress reporting
* Web client
* Python CLI

## v1.1 — Usability

* Drag-and-drop uploads
* Improved transfer queue
* Better CLI experience
* UI improvements

## v2.0 — Reliable Transfers

* Resume interrupted transfers
* Chunk acknowledgements
* Retry failed chunks
* Integrity verification
* Adaptive chunk sizing
* Transfer history

## v3.0 — Multi-Peer Rooms

* Multiple participants
* Group chat
* Broadcast messaging
* Peer management
* Multi-peer file distribution

## v4.0 — Security

* Password-protected rooms
* Application-layer end-to-end encryption
* Device verification
* TURN relay fallback
* Secure key verification

---

# v1.0 — Path to Deployment

Concrete work remaining to ship v1.0. Design decisions made along the
way are recorded in [docs/decisions.md](../decisions.md). Suggested order: Phase 1 → CLI chat
(8) → transfer (10) → STUN (6) → web client (12) → Docker (16).

## Phase 1 — Signaling server hardening

- [x] **1. Socket cleanup.** `ws_signaling` must always call `disconnect()`
      (`try/finally`), and `relay` must tolerate a dead peer socket, so a
      bad frame or failed send can't leak a registered socket or slot.
- [x] **2. Expiry sweeper.** Rooms only expire when looked up; a background
      task evicts expired rooms periodically so unrevisited rooms don't leak.
- [x] **3. Grace-period slot release.** After a WebSocket drops, call
      `leave_room` if the peer hasn't reconnected within a grace period,
      instead of holding the slot for the full room TTL.
- [x] **4. Abuse limits.** Per-IP rate limit on `POST /rooms`, WebSocket
      message size cap, relay only signaling types (`offer`, `answer`,
      `ice`), `Origin` check.
- [x] **5. Config via environment.** `WHOOSH_*` variables in
      `apps/server/config.py` (TTL, grace period, allowed origins, room
      creation rate, max message size, log level). ICE servers land with
      item 6. CLI no longer logs every ICE candidate check.

## Phase 2 — NAT traversal

- [x] **6. STUN.** `GET /config` returns ICE servers; both clients fetch it
      instead of hardcoding `iceServers=[]`.
- [x] **7. TURN.** Deferred to v4.0. Without it, peers behind symmetric NAT
      can't connect — clients must surface a clear "direct connection
      failed" error. (CLI done; web client must do the same.)

## Phase 3 — Protocol & transfer

- [x] **8. CLI speaks the protocol.** `hello` capability exchange on channel
      open, then an interactive stdin chat loop, via `packages/protocol/codec`.
- [x] **9. Surface handler errors.** `WebRTCPeer`'s `ensure_future` on
      incoming messages drops exceptions the same way the CLI listener did.
- [x] **10. `packages/transfer`.** `file_metadata` → chunks →
      `transfer_complete`. Binary frames (small header with transfer id and
      chunk index) instead of base64-in-JSON; backpressure on
      `bufferedAmount`; throttled `progress`; cancel; SHA-256 in
      `transfer_complete`.
- [x] **11. CLI commands.** `whoosh send FILE`, `whoosh receive ROOM_ID`;
      `[project.scripts]` entry for `uv tool install` / `pipx`.

## Phase 4 — Web client (`apps/web`)

- [x] **12.** React + TypeScript + Vite + Tailwind on native WebRTC.
- [x] **13.** TypeScript protocol types and runtime validation generated
      from the Pydantic models (`scripts/gen_ts_protocol.py`, decision D36).
- [x] **14.** Create/join (invite links), chat, ping, drag-and-drop send,
      receive with progress, Save links (Blob storage, decision D38).
- [x] **15.** Browser ↔ CLI and browser ↔ browser interop: automated
      Playwright suite in CI (decision D42; open issue I1).

## Phase 5 — Ship

- [x] **16. Docker.** Multi-stage `python:3.12-slim` + uv, server-only
      dependencies, non-root, health check. **Exactly one uvicorn
      worker**, because room state is in memory. Includes the built web
      client.
- [x] **17. docker-compose + Caddy.** Automatic HTTPS and WebSocket
      proxying (browsers require a secure context for WebRTC).
- [ ] **18. Hosting.** One small Docker VPS; guide in
      [docs/deployment.md](../deployment.md). *Needs a server and domain.*
- [x] **19. CI.** ruff, mypy (now clean), pytest (includes two real
      aiortc peers in-process), Docker build plus smoke test over real
      sockets. Web typecheck, tests and build; generated types
      checked for staleness.

---

# Milestone tracking

Where each GitHub issue is implemented and how it's verified. Design
choices that differ from an issue's wording are recorded in
[docs/decisions.md](../decisions.md).

## v1.0 — MVP

| Issue | Implementation | Verified by |
|---|---|---|
| #2 Room creation | `POST /rooms`, `packages/signaling/room_manager.py` | `tests/server/test_rooms.py` |
| #3 Room joining | `ConnectionManager.connect`, reconnection by `peer_id` | `tests/signaling/` |
| #4 WebSocket signaling | `/rooms/{id}/ws` relay (D2, D4, D5) | `tests/server/test_ws_signaling.py`, `scripts/smoke_test.py` |
| #5 SDP negotiation | Offer/answer over signaling: `packages/rtc/peer.py`, `apps/web/src/lib/connection.ts` | `tests/rtc/test_peer.py`, interop suite |
| #6 ICE candidate exchange | Candidates carried inside the SDP once gathering completes (D35); STUN servers served by `GET /config` (D10). The relay also accepts trickled `ice` messages | interop suite |
| #7 WebRTC DataChannel | One reliable, ordered channel per connection; ordered receive queue and backpressure (D21, D24) | `tests/rtc/test_peer.py` |
| #8 Peer-to-peer chat | `chat` messages through the shared protocol, both clients | `test_chat_and_ping_both_ways` |
| #9 Streaming large file transfer | `packages/transfer`, `apps/web/src/lib/transfer.ts`: chunked, never whole-file in memory on the sender | `tests/transfer/`, `transfer.test.ts`, interop suite |
| #10 Progress reporting | Progress callbacks on both sides; CLI progress line, web progress bars (D18) | `test_progress_updates_reach_completion` |
| #11 Web client | `apps/web` (React, TypeScript, Vite, Tailwind) | vitest, interop suite |
| #12 Python CLI | `apps/cli`: `create`/`join`/`send`/`receive`. Built on argparse rather than Typer (D27) | interop suite, `tests/` |

## v1.1 — Usability

| Issue | Implementation | Verified by |
|---|---|---|
| #13 Drag-and-drop uploads | Whole page is a drop target with an overlay (D44) | interop suite (drops dispatched to the page) |
| #14 Improved transfer queue | `SendQueue` in both clients: ordered, one at a time, removable (D43); CLI `/queue` | `queue.test.ts`, `tests/cli/test_cli.py` |
| #15 Better CLI experience | Progress bar with speed/ETA, colour (TTY only, `NO_COLOR`), friendly errors, room codes or invite links as arguments, help when run bare (D45) | `tests/cli/test_cli.py` |
| #16 UI improvements | Latency in the status bar, unread count in the tab title, ETA per transfer, confirm before leaving mid-transfer, **Try again** after a failed connection, invite links accepted in the join box, empty state | interop suite, screenshots |

## v2.0 — Reliable Transfers

| Issue | Implementation | Verified by |
|---|---|---|
| #17 Resume interrupted transfers | `resume_key` + `transfer_accept {offset}` (D46); partials on disk (CLI) or in the tab (web) (D47); dead-peer detection (D48) | `tests/transfer/test_resume.py`, `resume.test.ts`, interop: page closed mid-upload / CLI killed mid-send |
