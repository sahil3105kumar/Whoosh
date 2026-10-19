import { useCallback, useEffect, useRef, useState } from "react";

import { type ConnectPhase, connect } from "./lib/connection";
import { type QueuedFile, SendQueue } from "./lib/queue";
import { Session } from "./lib/session";
import type { Transfer } from "./lib/transfer";

export type Phase = ConnectPhase | "connected" | "closed" | "error";

export interface LogEntry {
  id: number;
  from: "me" | "peer" | "system";
  text: string;
}

/** How often to measure latency while connected (shown in the status bar). */
const LATENCY_INTERVAL_MS = 10_000;

let nextLogId = 0;

/** Connection + session lifecycle for one room, as React state. */
export function useRoom(roomId: string, isCreator: boolean) {
  const [phase, setPhase] = useState<Phase>("signaling");
  const [error, setError] = useState<string>();
  const [log, setLog] = useState<LogEntry[]>([]);
  const [transfers, setTransfers] = useState<Transfer[]>([]);
  const [queued, setQueued] = useState<readonly QueuedFile[]>([]);
  const [peerReady, setPeerReady] = useState(false);
  const [latencyMs, setLatencyMs] = useState<number>();
  const sessionRef = useRef<Session>(undefined);
  const queueRef = useRef<SendQueue>(undefined);
  // A Ping button press reports its result in the chat; background pings don't
  const manualPing = useRef(false);

  const addLog = useCallback((from: LogEntry["from"], text: string) => {
    setLog((entries) => [...entries, { id: nextLogId++, from, text }]);
  }, []);

  useEffect(() => {
    const abort = new AbortController();
    let session: Session | undefined;
    let ws: WebSocket | undefined;
    let pinger: ReturnType<typeof setInterval> | undefined;

    const queue = new SendQueue(async (file) => {
      if (session) {
        await session.transfers.sendFile(file, { resume: session.peerSupports("resume") });
      }
    }, setQueued);
    queueRef.current = queue;

    connect(roomId, isCreator, setPhase, abort.signal)
      .then((conn) => {
        ws = conn.ws;
        session = new Session(conn.channel, {
          onChat: (text) => addLog("peer", text),
          onNotice: (text) => addLog("system", text),
          onTransfer: (t) =>
            setTransfers((list) => {
              const i = list.findIndex((x) => x.id === t.id);
              return i === -1 ? [...list, t] : list.with(i, t);
            }),
          onPeerReady: () => setPeerReady(true),
          onLatency: (ms) => {
            setLatencyMs(ms);
            if (manualPing.current) {
              manualPing.current = false;
              addLog("system", `Round trip: ${ms.toFixed(1)} ms`);
            }
          },
          onClosed: () => {
            clearInterval(pinger);
            setLatencyMs(undefined);
            const dropped = queue.clear().length;
            if (dropped) addLog("system", `${dropped} queued file(s) weren't sent.`);
            setPhase((p) => (p === "connected" ? "closed" : p));
          },
        });
        sessionRef.current = session;
        setPhase("connected");
        session.ping();
        pinger = setInterval(() => session?.ping(), LATENCY_INTERVAL_MS);
      })
      .catch((err: unknown) => {
        if (abort.signal.aborted) return;
        setError((err as Error).message);
        setPhase("error");
      });

    return () => {
      abort.abort();
      clearInterval(pinger);
      queue.clear();
      session?.close();
      ws?.close();
      sessionRef.current = undefined;
    };
  }, [roomId, isCreator, addLog]);

  const sendChat = useCallback(
    (text: string) => {
      sessionRef.current?.sendChat(text);
      addLog("me", text);
    },
    [addLog],
  );

  const sendFiles = useCallback((files: File[]) => queueRef.current?.add(files), []);
  const unqueue = useCallback((id: string) => queueRef.current?.remove(id), []);
  const cancel = useCallback((id: string) => sessionRef.current?.transfers.cancel(id), []);
  const ping = useCallback(() => {
    manualPing.current = true;
    sessionRef.current?.ping();
  }, []);

  const busy = queued.length > 0 || transfers.some((t) => t.status === "streaming");

  return {
    phase,
    error,
    log,
    transfers,
    queued,
    peerReady,
    latencyMs,
    busy,
    sendChat,
    sendFiles,
    unqueue,
    cancel,
    ping,
  };
}
