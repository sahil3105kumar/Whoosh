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
* Typer
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

## Documentation

* Vision
* PRD
* Architecture
* Protocol
* Roadmap

## Roadmap

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
