# Whoosh System Design

> This document defines the complete architecture of Whoosh.
>
> It is the single source of truth for the system design. Every implementation must conform to the principles and architecture described here.
>
> The implementation may evolve, but the architecture should remain stable. Changes to this document represent architectural decisions rather than implementation details.

---

# 1. Purpose

Whoosh is a peer-to-peer communication platform designed to demonstrate modern networking concepts through practical implementation.

The project focuses on:

- WebSockets
- WebRTC
- SDP negotiation
- ICE candidate exchange
- STUN
- Reliable DataChannels
- Streaming large files
- Protocol design
- Distributed systems
- Network programming

Whoosh is intended to be educational while maintaining production-quality software engineering practices.

---

# 2. Design Principles

The following principles are immutable.

## Peer-to-Peer First

Once peers establish a WebRTC connection, the server no longer participates in communication.

All application traffic flows directly between peers.

---

## Server as Signaling Only

The server is responsible only for:

- room creation
- room discovery
- signaling
- room lifecycle
- heartbeats

The server never:

- stores files
- relays application traffic
- inspects transferred data
- persists user content

---

## Shared Protocol

Every client communicates using the same protocol.

The transport may differ.

Examples:

- Browser → Native WebRTC
- CLI → aiortc
- Mobile → Native implementation

The protocol remains identical.

---

## Transport Independence

The application layer must never depend directly on WebRTC.

All communication passes through a transport abstraction.

Future transports should be possible without changing application logic.

Examples:

- WebRTC
- QUIC
- TCP

---

## Streaming First

Large files must never be loaded entirely into memory.

Every transfer is streamed.

Memory usage should remain approximately constant regardless of file size.

---

## Async First

Every network operation is asynchronous.

Blocking operations should not exist within networking components.

---

## Modular Architecture

Each package owns a single responsibility.

Dependencies always point downward.

Circular dependencies are forbidden.

---

# 3. High-Level Architecture

```
                  Browser
                      │
                WebSocket
                      │
         ┌────────────────────────┐
         │ FastAPI Signaling      │
         └────────────────────────┘
                      │
                WebSocket
                      │
                   Python CLI


Offer
Answer
ICE

↓

WebRTC

↓

Reliable DataChannel

↓

Application Protocol

↓

Chat
File Transfer
Future Features
```

---

# 4. System Components

The system consists of four major parts.

## Clients

Responsible for:

- user interface
- transport initialization
- rendering state
- user interaction

Implementations:

- Web
- CLI
- Future Mobile

---

## Signaling Server

Responsible for:

- room management
- websocket signaling
- SDP relay
- ICE relay
- room expiration

The signaling server is stateless with respect to user data.

---

## Shared Packages

Contain reusable business logic shared across applications.

Examples:

- protocol
- rtc
- transfer
- signaling

---

## Infrastructure

External services.

Examples:

- STUN
- TURN (future)

---

# 5. Package Architecture

```
apps/

    server

    web

    cli


packages/

    common

    protocol

    signaling

    rtc

    transfer

    crypto

    compression

    types
```

Not every package is implemented in v1.

Packages may exist before they contain code.

---

# 6. Dependency Rules

Dependencies always point downward.

```
Applications

↓

Transfer

↓

RTC

↓

Protocol

↓

Common
```

Never:

```
Protocol

↓

RTC
```

or

```
Transfer

↓

Web
```

---

# 7. Domain Model

Core domain entities.

```
Room

Peer

Session

Connection

Transfer

Chunk

Capability

Message

Offer

Answer

ICE Candidate
```

These entities define the language of the system.

New features should extend existing entities rather than introducing parallel concepts.

---

# 8. Communication Flow

## Connection

```
Create Room

↓

Join Room

↓

WebSocket

↓

Offer

↓

Answer

↓

ICE

↓

DataChannel

↓

Peer Connected
```

---

## Chat

```
Chat Message

↓

Protocol

↓

Transport

↓

Remote Peer
```

---

## File Transfer

```
File

↓

Metadata

↓

Chunk Stream

↓

Progress

↓

Completion
```

---

# 9. Protocol

Every application message follows a common envelope.

```
Envelope

↓

Payload
```

Example:

```json
{
    "version": 1,
    "id": "...",
    "type": "chat",
    "payload": {}
}
```

The envelope remains stable.

Only payloads evolve.

---

# 10. Transport

Transport is responsible only for moving bytes.

It knows nothing about:

- chat
- files
- rooms
- transfers

Responsibilities:

- send
- receive
- reconnect
- connection state

---

# 11. State Machines

The system contains multiple independent state machines.

## Room

```
Created

↓

Waiting

↓

Connected

↓

Expired
```

---

## Connection

```
Connecting

↓

Negotiating

↓

Connected

↓

Disconnected

↓

Closed
```

---

## Transfer

```
Pending

↓

Metadata

↓

Streaming

↓

Completed
```

Future versions extend this state machine with:

- paused
- resumed
- retrying

---

# 12. Public Interfaces

Every major subsystem exposes a single public interface.

Examples:

```
ProtocolCodec

RoomManager

TransferManager

PeerConnection

SignalingClient
```

Applications depend on interfaces rather than implementations.

---

# 13. Extension Points

The architecture intentionally reserves extension points.

Future versions may introduce:

- resume
- acknowledgements
- encryption
- compression
- TURN relay
- multi-peer rooms

No redesign should be required.

---

# 14. Architectural Invariants

The following rules are never violated.

- The signaling server never transfers user data.
- All peer communication occurs through the protocol layer.
- Files are always streamed.
- Protocol messages are versioned.
- Application code never depends directly on WebRTC.
- Dependencies always point downward.
- Every milestone produces a working application.
- The implementation may evolve, but the architecture remains stable.

---

# 15. Versioning Strategy

The architecture represents the complete vision of Whoosh.

Each release implements a subset of this architecture.

```
v0.1

Protocol

↓

v0.2

Signaling

↓

v0.3

RTC

↓

v0.4

Chat

↓

v0.5

Streaming

↓

v1.0

Stable

↓

v2+

Extensions
```

No release should require architectural redesign.