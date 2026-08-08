# Protocol

All peer communication occurs over the WebRTC DataChannel.

Every message follows the same envelope.

```json
{
  "version": 1,
  "type": "message_type",
  "id": "uuid",
  "payload": {}
}
```

## Message Types

### hello

Capability negotiation between peers.

### chat

Text message.

### file_metadata

Information about an incoming file.

### file_chunk

Binary chunk belonging to a transfer.

### progress

Transfer progress update.

### transfer_complete

Signals successful completion.

### transfer_cancel

Cancels an active transfer.

### error

Reports protocol or transfer errors.

### ping

Latency measurement.

### pong

Ping response.

## Versioning

Every protocol message includes a version number to allow backward-compatible evolution.

Future protocol versions may introduce new capabilities while maintaining compatibility with older clients.
