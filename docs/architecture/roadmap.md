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

Concrete work remaining to ship v1.0. Suggested order: Phase 1 → CLI chat
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

- [ ] **8. CLI speaks the protocol.** `hello` capability exchange on channel
      open, then an interactive stdin chat loop, via `packages/protocol/codec`.
- [ ] **9. Surface handler errors.** `WebRTCPeer`'s `ensure_future` on
      incoming messages drops exceptions the same way the CLI listener did.
- [ ] **10. `packages/transfer`.** `file_metadata` → chunks →
      `transfer_complete`. Binary frames (small header with transfer id and
      chunk index) instead of base64-in-JSON; backpressure on
      `bufferedAmount`; throttled `progress`; cancel; SHA-256 in
      `transfer_complete`.
- [ ] **11. CLI commands.** `whoosh send FILE`, `whoosh receive ROOM_ID`;
      `[project.scripts]` entry for `uv tool install` / `pipx`.

## Phase 4 — Web client (`apps/web`)

- [ ] **12.** React + TypeScript + Vite on native WebRTC.
- [ ] **13.** TypeScript protocol types generated from the Pydantic models
      (`model_json_schema()` → `json-schema-to-typescript`).
- [ ] **14.** Create/join, chat, send/receive with progress (File System
      Access API, Blob fallback).
- [ ] **15.** Browser ↔ CLI interop test.

## Phase 5 — Ship

- [ ] **16. Docker.** Multi-stage: build web → `python:3.12-slim` + uv
      serving API and static files. **Exactly one uvicorn worker** — room
      state is in memory.
- [ ] **17. docker-compose + Caddy.** Automatic HTTPS and WebSocket
      proxying (browsers require a secure context for WebRTC).
- [ ] **18. Hosting.** One small VPS or a single Fly.io machine.
- [ ] **19. CI.** ruff, mypy (clean up existing errors), pytest, web
      lint/build, Docker build, and an in-process two-peer end-to-end test.
