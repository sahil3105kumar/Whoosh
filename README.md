# Whoosh

> A lightweight peer-to-peer communication platform built to learn modern networking.

Whoosh is a production-quality implementation of peer-to-peer communication using **WebRTC**. It allows two peers to establish a direct connection through a lightweight signaling server and then communicate without the server participating in data transfer.

The project focuses on understanding how modern networking works rather than building another cloud storage service.

## Features

* Temporary rooms
* No accounts or authentication
* WebSocket signaling
* WebRTC peer connections
* Direct peer-to-peer chat
* Streaming large file transfers
* Cross-platform Web application
* Python CLI client
* Shared application protocol
* Modular architecture

## Architecture

```
Browser / CLI
      │
 WebSocket Signaling
      │
 FastAPI Server
      │
 SDP + ICE Exchange
      │
 WebRTC DataChannel
      │
 Shared Application Protocol
      │
 Chat & File Transfer
```

After the WebRTC connection is established, the signaling server is no longer involved. All communication happens directly between peers.

## Technology Stack

### Backend

* Python 3.12+
* FastAPI
* asyncio
* WebSockets

### Web

* React
* TypeScript
* Vite
* TailwindCSS
* Native WebRTC APIs

### CLI

* Python
* argparse
* aiortc

### Testing

* pytest

### Deployment

* Docker
* Reverse Proxy
* HTTPS

## Repository Structure

```
whoosh/
├── apps/
│   ├── server/
│   ├── web/
│   └── cli/
├── packages/
│   ├── protocol/
│   ├── rtc/
│   ├── signaling/
│   ├── transfer/
│   ├── common/
│   └── types/
├── docs/
├── tests/
├── docker/
└── scripts/
```

## Quick start

Install the CLI:

```bash
uv tool install "whoosh[cli] @ git+https://github.com/sahil3105kumar/Whoosh"
```

Send a file:

```bash
export WHOOSH_SERVER=https://your.whoosh.server
whoosh send report.pdf            # prints a room code
whoosh receive <room code>        # on the other machine
```

Or chat: `whoosh create` / `whoosh join <room code>`, then `/send PATH`.
Rooms hold up to 8 people: chat goes to everyone, and a file sent in a
room goes to everyone in it (`whoosh send --peers 3 file.zip` waits for
three people). Pick a display name with `--name`.

`whoosh history` lists past transfers (kept only on your machine).

Everything is end-to-end encrypted. To be sure nobody is in the middle
(not even the server), compare the 6 emoji each side shows — `/verify`
in the CLI, **Verify** in the browser — once per device. Rooms can have a
password (`--password`), and a TURN relay can be enabled for networks
that block direct connections (see [deployment](docs/deployment.md)).

Interrupted transfers resume where they stopped, and a dropped
connection is re-established automatically.

Or use the web app: open the server's address in a browser, create a
room, and share the invite link. It works with CLI peers too.

## Development

```bash
uv sync                                    # Python: server, CLI, tests
uv run uvicorn apps.server.main:app        # signaling server on :8000
cd apps/web && npm install && npm run dev  # web app on :5173, proxied to :8000
```

After changing `packages/protocol`, regenerate the web client's types
with `uv run python scripts/gen_ts_protocol.py`.

Browser ↔ CLI interop tests (headless Chromium):

```bash
uv sync --group interop && uv run playwright install chromium
(cd apps/web && npm run build)
uv run pytest tests/interop
```

Deploy with Docker; see [docs/deployment.md](docs/deployment.md).

## Documentation

* Vision
* PRD
* Architecture
* Protocol
* Roadmap
* [Decisions](docs/decisions.md)
* [Deployment](docs/deployment.md)

## Roadmap

### Done

v1.0 (MVP), v1.1 (usability), v2.0 (reliable transfers), v3.0
(multi-peer rooms) and v4.0 (security). See
[docs/architecture/roadmap.md](docs/architecture/roadmap.md) for what each
issue became, and [docs/decisions.md](docs/decisions.md) for why.

### v1.0

* Room creation
* Room joining
* WebSocket signaling
* SDP negotiation
* ICE exchange
* WebRTC DataChannel
* Peer-to-peer chat
* Streaming file transfer

### Future

* Resume transfers
* Multi-peer rooms
* Group chat
* End-to-end encryption
* TURN relay support

## License

MIT
