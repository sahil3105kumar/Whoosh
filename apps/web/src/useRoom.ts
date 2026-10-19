import { useCallback, useEffect, useRef, useState } from "react";

import { type ConnectPhase, Link, type PeerChannel } from "./lib/connection";
import { recordTransfer } from "./lib/history";
import { type QueuedFile, SendQueue } from "./lib/queue";
import { Session } from "./lib/session";
import type { Transfer } from "./lib/transfer";

export type Phase = ConnectPhase | "connected" | "reconnecting" | "closed" | "error";

export interface LogEntry {
  id: number;
  from: "me" | "peer" | "system";
  text: string;
}

/** How often to measure latency while connected (shown in the status bar). */
const LATENCY_INTERVAL_MS = 10_000;
/** Attempts per file when the connection keeps dropping (#19). */
const MAX_SEND_ATTEMPTS = 3;
/** How long a retry waits for the reconnect. */
const RETRY_WAIT_MS = 90_000;

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
    const link = new Link(roomId, isCreator, setPhase);
    let pinger: ReturnType<typeof setInterval> | undefined;
    let alive = true;

    const session = new Session(
      {
        onChat: (text) => addLog("peer", text),
        onNotice: (text) => addLog("system", text),
        onTransfer: (t) => {
          if (t.status !== "streaming") recordTransfer(t); // reported once, when it ends
          setTransfers((list) => {
            const i = list.findIndex((x) => x.id === t.id);
            return i === -1 ? [...list, t] : list.with(i, t);
          });
        },
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
          setPeerReady(false);
          if (!alive) return;
          if (session.peerLeft) {
            const dropped = queue.clear().length;
            if (dropped) addLog("system", `${dropped} queued file(s) weren't sent.`);
            setPhase("closed");
            return;
          }
          // Not a goodbye: the connection dropped. Get it back (D50)
          setPhase("reconnecting");
          link
            .reconnect()
            .then(attach)
            .catch((err: unknown) => {
              if (!alive) return;
              queue.clear();
              setError((err as Error).message);
              setPhase("closed");
            });
        },
      },
      () => link.drop(), // a transfer stalled: the connection is dead
    );
    sessionRef.current = session;

    // Interrupted sends are retried after reconnecting; resume means only
    // the missing part goes again (#19)
    const queue = new SendQueue(async (file) => {
      for (let attempt = 1; attempt <= MAX_SEND_ATTEMPTS; attempt++) {
        const generation = session.generation;
        const t = await session.transfers.sendFile(file, session.sendOptions());
        if (!t.interrupted || attempt === MAX_SEND_ATTEMPTS) return;
        if (!(await session.waitReady(generation, RETRY_WAIT_MS))) return;
      }
    }, setQueued);
    queueRef.current = queue;

    const attach = (channel: PeerChannel) => {
      if (!alive) return;
      session.attach(channel);
      setError(undefined);
      setPhase("connected");
      session.ping();
      clearInterval(pinger);
      pinger = setInterval(() => session.ping(), LATENCY_INTERVAL_MS);
    };

    // Closing the tab is leaving on purpose, not a network failure
    const onPageHide = () => session.sayBye();
    addEventListener("pagehide", onPageHide);

    link
      .open(abort.signal)
      .then(attach)
      .catch((err: unknown) => {
        if (abort.signal.aborted) return;
        setError((err as Error).message);
        setPhase("error");
      });

    return () => {
      alive = false;
      removeEventListener("pagehide", onPageHide);
      session.sayBye();
      abort.abort();
      clearInterval(pinger);
      queue.clear();
      // Give the bye a moment to leave before the connection closes
      setTimeout(() => {
        session.close();
        link.close();
      }, 100);
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
