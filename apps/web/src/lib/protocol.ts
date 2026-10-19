/**
 * Application protocol envelope: encode outgoing messages and validate
 * incoming ones. Types and field specs are generated from the Python
 * models (protocol.gen.ts), so both clients speak exactly one protocol.
 */
import {
  type FieldKind,
  type MessageMap,
  type MessageType,
  PAYLOAD_FIELDS,
  PROTOCOL_VERSION,
} from "./protocol.gen";

export * from "./protocol.gen";

/** Discriminated union: narrowing on `type` narrows `payload`. */
export type ProtocolMessage = {
  [K in MessageType]: { version: number; id: string; type: K; payload: MessageMap[K] };
}[MessageType];

export class ProtocolError extends Error {}

export function encodeMessage<K extends MessageType>(type: K, payload: MessageMap[K]): string {
  return JSON.stringify({ version: PROTOCOL_VERSION, id: crypto.randomUUID(), type, payload });
}

/** Parse and validate a text frame. Throws ProtocolError. */
export function decodeMessage(text: string): ProtocolMessage {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (err) {
    throw new ProtocolError(`invalid JSON: ${(err as Error).message}`);
  }
  if (!isObject(raw)) throw new ProtocolError("expected a JSON object");

  const { version, type, id, payload } = raw;
  if (!Number.isInteger(version)) throw new ProtocolError("missing or non-integer 'version'");
  if ((version as number) > PROTOCOL_VERSION) {
    throw new ProtocolError(`unsupported protocol version ${String(version)}`);
  }
  if (typeof type !== "string" || !Object.hasOwn(PAYLOAD_FIELDS, type)) {
    throw new ProtocolError(`unknown message type ${JSON.stringify(type)}`);
  }
  if (typeof id !== "string") throw new ProtocolError("missing 'id'");
  if (!isObject(payload)) throw new ProtocolError("payload must be an object");

  for (const [name, spec] of Object.entries(PAYLOAD_FIELDS[type as MessageType])) {
    const value = payload[name];
    if (value === undefined && spec.optional) continue;
    if (!matches(value, spec.kind)) {
      throw new ProtocolError(`${type}.${name}: expected ${spec.kind}`);
    }
  }
  return raw as ProtocolMessage;
}

function matches(value: unknown, kind: FieldKind): boolean {
  switch (kind) {
    case "string":
      return typeof value === "string";
    case "integer":
      return Number.isSafeInteger(value);
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "boolean":
      return typeof value === "boolean";
    case "string[]":
      return Array.isArray(value) && value.every((v) => typeof v === "string");
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
