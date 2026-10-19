import { useCallback, useEffect, useRef, useState } from "react";

import { type ConnectPhase, connect } from "./lib/connection";
import { Session } from "./lib/session";
import type { Transfer } from "./lib/transfer";

export type Phase = ConnectPhase | "connected" | "closed" | "error";

export interface LogEntry {
  id: number;
  from: "me" | "peer" | "system";
  text: string;
}

let nextLogId = 0;

/** Connection + session lifecycle for one room, as React state. */
export function useRoom(roomId: string, isCreator: boolean) {
  const [phase, setPhase] = useState<Phase>("signaling");
  const [error, setError] = useState<string>();
  const [log, setLog] = useState<LogEntry[]>([]);
  const [transfers, setTransfers] = useState<Transfer[]>([]);
  const [peerReady, setPeerReady] = useState(false);
  const sessionRef = useRef<Session>(undefined);

  const addLog = useCallback((from: LogEntry["from"], text: string) => {
    setLog((entries) => [...entries, { id: nextLogId++, from, text }]);
  }, []);

  useEffect(() => {
    const abort = new AbortController();
    let session: Session | undefined;
    let ws: WebSocket | undefined;

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
          onClosed: () => setPhase((p) => (p === "connected" ? "closed" : p)),
        });
        sessionRef.current = session;
        setPhase("connected");
      })
      .catch((err: unknown) => {
        if (abort.signal.aborted) return;
        setError((err as Error).message);
        setPhase("error");
      });

    return () => {
      abort.abort();
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

  const sendFiles = useCallback(async (files: File[]) => {
    // One at a time: concurrent sends would just share the same channel
    for (const file of files) {
      const session = sessionRef.current;
      if (!session) return;
      await session.transfers.sendFile(file);
    }
  }, []);

  const cancel = useCallback((id: string) => sessionRef.current?.transfers.cancel(id), []);
  const ping = useCallback(() => sessionRef.current?.ping(), []);

  return { phase, error, log, transfers, peerReady, sendChat, sendFiles, cancel, ping };
}
