# Vision

Whoosh exists to demonstrate how modern peer-to-peer networking works through a clean, production-quality implementation.

The project is intentionally designed around temporary communication sessions rather than persistent cloud services. Users create a room, establish a direct WebRTC connection, exchange messages and files, and leave no permanent state behind.

The backend is only responsible for signaling. Once peers connect, all communication occurs directly between devices.

Whoosh emphasizes learning through implementation. Every architectural decision should expose networking concepts such as WebSockets, SDP negotiation, ICE candidate exchange, STUN, DataChannels, streaming, and reliable transport while remaining approachable for a solo developer.

Documentation is intentionally minimal. The implementation is the primary source of truth, with only a small set of documents maintained throughout the project's lifetime.
