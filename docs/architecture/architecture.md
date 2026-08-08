# Architecture

## Principles

* Peer-to-peer first
* Server used only for signaling
* Async-first implementation
* Modular packages
* Shared protocol across all clients
* Streaming instead of buffering
* Extensible without major redesign

## Components

### Signaling Server

Responsibilities:

* Create rooms
* Join rooms
* Relay SDP offers
* Relay SDP answers
* Relay ICE candidates
* Heartbeats
* Room expiration

The signaling server never stores or transfers user data.

### RTC Layer

Responsible for establishing and maintaining WebRTC peer connections.

### Protocol Layer

Defines all messages exchanged between peers. It is transport-agnostic and independent of WebRTC.

### Transfer Engine

Provides streaming file transfer with progress reporting, cancellation, and future support for resume and integrity verification.

### Clients

Both the Web application and Python CLI implement the same protocol and differ only in presentation and transport bindings.

## System Flow

```
Create Room
      ↓
Join Room
      ↓
WebSocket Signaling
      ↓
Offer / Answer
      ↓
ICE Exchange
      ↓
WebRTC DataChannel
      ↓
Peer-to-Peer Communication
```
