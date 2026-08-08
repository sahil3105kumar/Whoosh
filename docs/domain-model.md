# Whoosh Domain Model

> This document defines the core domain entities, their responsibilities,
> ownership, relationships, and lifecycles.
>
> It complements `system-design.md` and is intended to remain stable across
> the entire Whoosh project.
>
> This document describes the complete domain model, including concepts that
> may not be implemented until later versions.

---

# 1. Domain Overview

Whoosh consists of two primary domains:

1. **Session Domain** — rooms, peers, sessions, and connections.
2. **Transfer Domain** — transfers, file metadata, and streamed data.

The protocol layer represents these concepts as messages exchanged between
peers, but the protocol does not own their lifecycle.

```text
                    WHOOSH
                       │
          ┌────────────┴────────────┐
          │                         │
     Session Domain           Transfer Domain
          │                         │
         Room                    Transfer
          │                         │
         Peer                 File Metadata
          │                         │
       Session                    Chunks
          │
      Connection