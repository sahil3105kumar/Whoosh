import { describe, expect, it } from "vitest";

import { safeFilename } from "./filenames";
import { FrameDecodeError, decodeFrame, encodeChunk } from "./frames";
import { ProtocolError, decodeMessage, encodeMessage } from "./protocol";

const TID = "0123456789abcdef0123456789abcdef";

const hex = (bytes: Uint8Array) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");

describe("frames", () => {
  it("encodes byte-for-byte like the Python implementation", () => {
    // From packages/transfer/frames.py: encode_chunk(TID, 2**40 + 5, b"payload").hex()
    const python = "010123456789abcdef0123456789abcdef00000100000000057061796c6f6164";
    const frame = encodeChunk(TID, 2 ** 40 + 5, new TextEncoder().encode("payload"));
    expect(hex(frame)).toBe(python);
  });

  it("round-trips", () => {
    const frame = encodeChunk(TID, 123, new Uint8Array([1, 2, 3]));
    const decoded = decodeFrame(frame.buffer as ArrayBuffer);
    expect(decoded.transferId).toBe(TID);
    expect(decoded.offset).toBe(123);
    expect([...decoded.data]).toEqual([1, 2, 3]);
  });

  it.each([new Uint8Array(0), new Uint8Array(10), new Uint8Array(25).fill(2)])(
    "rejects bad frames",
    (bytes) => {
      expect(() => decodeFrame(bytes.buffer as ArrayBuffer)).toThrow(FrameDecodeError);
    },
  );
});

describe("protocol", () => {
  it("round-trips a message", () => {
    const msg = decodeMessage(encodeMessage("chat", { text: "hi" }));
    expect(msg.type).toBe("chat");
    if (msg.type === "chat") expect(msg.payload.text).toBe("hi");
  });

  it("accepts optional fields being absent", () => {
    const text = JSON.stringify({ version: 1, id: "x", type: "transfer_cancel", payload: { transfer_id: "t" } });
    expect(decodeMessage(text).type).toBe("transfer_cancel");
  });

  it.each([
    ["not json", "{"],
    ["non-object", "[1]"],
    ["future version", JSON.stringify({ version: 99, id: "x", type: "chat", payload: { text: "" } })],
    ["unknown type", JSON.stringify({ version: 1, id: "x", type: "nope", payload: {} })],
    ["prototype key as type", JSON.stringify({ version: 1, id: "x", type: "toString", payload: {} })],
    ["missing field", JSON.stringify({ version: 1, id: "x", type: "chat", payload: {} })],
    [
      "wrong field type",
      JSON.stringify({ version: 1, id: "x", type: "file_metadata", payload: { transfer_id: "t", filename: "f", size: "big" } }),
    ],
    [
      "non-integer size",
      JSON.stringify({ version: 1, id: "x", type: "file_metadata", payload: { transfer_id: "t", filename: "f", size: 1.5 } }),
    ],
  ])("rejects %s", (_, text) => {
    expect(() => decodeMessage(text)).toThrow(ProtocolError);
  });
});

describe("safeFilename (same cases as the Python tests)", () => {
  it.each([
    ["report.pdf", "report.pdf"],
    ["../../etc/passwd", "passwd"],
    ["C:\\Users\\x\\evil.exe", "evil.exe"],
    ["..", "download"],
    ["", "download"],
    ["a\x00b\nc.txt", "a_b_c.txt"],
    ['what?:*"<>|.txt', "what_______.txt"],
  ])("%j → %j", (given, expected) => {
    expect(safeFilename(given)).toBe(expected);
  });

  it("truncates long names keeping the extension", () => {
    const name = safeFilename("x".repeat(400) + ".tar.gz");
    expect(new TextEncoder().encode(name).length).toBeLessThanOrEqual(255);
    expect(name.endsWith(".gz")).toBe(true);
  });
});
