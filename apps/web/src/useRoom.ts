import { useCallback, useEffect, useRef, useState } from "react";

import { type ConnectPhase, Mesh } from "./lib/connection";
import { recordTransfer } from "./lib/history";
import { type QueuedFile, SendQueue } from "./lib/queue";
import { TrustStore, loadIdentity } from "./lib/identity";
import { type Security, Session } from "./lib/session";
import type { Transfer } from "./lib/transfer";

export type Phase = ConnectPhase | "connected" | "reconnecting" | "closed" | "error";

export interface LogEntry {
  id: number;
  from: "me" | "peer" | "system";
  /** Who sent it, for messages from peers. */
  name?: string;
  text: string;
}

export interface Peer {
  id: string;
  name: string;
  connected: boolean;
  /** Encryption and verification state, once the key exchange is done. */
  security?: Security;
}

/** A transfer, and who it's with. */
export interface PeerTransfer extends Transfer {
  key: string;
  peerName: string;
}

/** How often to measure latency while connected (shown in the status bar). */
const LATENCY_INTERVAL_MS = 10_000;
/** How long a peer whose connection dropped (no bye) gets to come back. */
const RECONNECT_TIMEOUT_MS = 60_000;
/** Attempts per file per peer when the connection keeps dropping (#19). */
const MAX_SEND_ATTEMPTS = 3;
/** How long a retry waits for the reconnect. */
const RETRY_WAIT_MS = 90_000;

let nextLogId = 0;

/** Our display name, chosen on the home page (#26). */
export function savedName(): string {
  try {
    return localStorage.getItem("whoosh.name") ?? "";
  } catch {
    return "";
  }
}

/** Everyone in one room, as React state: one Session per peer (#23–#27). */
export function useRoom(roomId: string, password?: string) {
  const [meshPhase, setMeshPhase] = useState<ConnectPhase>("signaling");
  const [error, setError] = useState<string>();
  const [log, setLog] = useState<LogEntry[]>([]);
  const [transfers, setTransfers] = useState<PeerTransfer[]>([]);
  const [queued, setQueued] = useState<readonly QueuedFile[]>([]);
  const [peers, setPeers] = useState<Peer[]>([]);
  const [everConnected, setEverConnected] = useState(false);
  const [latencyMs, setLatencyMs] = useState<number>();
  const sessionsRef = useRef(new Map<string, Session>());
  const queueRef = useRef<SendQueue>(undefined);
  // A Ping button press reports its result in the chat; background pings don't
  const manualPing = useRef(false);

  const addLog = useCallback((from: LogEntry["from"], text: string, name?: string) => {
    setLog((entries) => [...entries, { id: nextLogId++, from, text, name }]);
  }, []);

  useEffect(() => {
    const abort = new AbortController();
    const sessions = sessionsRef.current;
    const lostTimers = new Map<string, ReturnType<typeof setTimeout>>();
    const latencies = new Map<string, number>();
    const name = savedName();
    let alive = true;

    const trust = new TrustStore();
    const identity = loadIdentity(); // this device's key, from IndexedDB (#30)
    const updatePeers = () =>
      setPeers(
        [...sessions].map(([id, s]) => ({
          id,
          name: s.peerName,
          connected: s.connected,
          security: s.security,
        })),
      );

    const remove = (peerId: string, why: string) => {
      const session = sessions.get(peerId);
      clearTimeout(lostTimers.get(peerId));
      lostTimers.delete(peerId);
      latencies.delete(peerId);
      if (!session) return;
      sessions.delete(peerId);
      session.transfers.close("peer left");
      addLog("system", `${session.peerName} ${why}`);
      updatePeers();
    };

    const createSession = async (peerId: string): Promise<Session> => {
      const deviceIdentity = await identity;
      let introduced = false;
      const session: Session = new Session(
        {
          onChat: (text) => addLog("peer", text, session.peerName),
          onNotice: (text) => addLog("system", text),
          onTransfer: (t) => {
            if (t.status !== "streaming") recordTransfer(t); // reported once, when it ends
            const key = `${peerId}:${t.id}`;
            const row: PeerTransfer = { ...t, key, peerName: session.peerName };
            setTransfers((list) => {
              const i = list.findIndex((x) => x.key === key);
              return i === -1 ? [...list, row] : list.with(i, row);
            });
          },
          onPeerReady: () => {
            if (!introduced) addLog("system", `${session.peerName} joined.`);
            introduced = true;
            updatePeers();
          },
          onLatency: (ms) => {
            latencies.set(peerId, ms);
            setLatencyMs(Math.max(...latencies.values()));
            if (manualPing.current) {
              manualPing.current = false;
              addLog("system", `Round trip: ${ms.toFixed(1)} ms`);
            }
          },
          onSecurity: (security) => {
            if (!security.verified) {
              addLog("system", `🔒 ${session.peerName}: encrypted. Compare codes to verify.`);
            }
            updatePeers();
          },
          onClosed: () => {
            latencies.delete(peerId);
            updatePeers();
            if (!alive) return;
            if (session.peerLeft) {
              mesh.forget(peerId);
              remove(peerId, session.refused ? "was disconnected." : "left.");
              return;
            }
            // Not a goodbye: the connection dropped. The mesh reconnects (D50)
            addLog("system", `Connection to ${session.peerName} lost. Reconnecting…`);
            lostTimers.set(
              peerId,
              setTimeout(() => {
                mesh.forget(peerId);
                remove(peerId, "didn't come back.");
              }, RECONNECT_TIMEOUT_MS),
            );
          },
        },
        () => mesh.drop(peerId), // a transfer stalled: that connection is dead
        name,
        peerId,
        deviceIdentity,
        trust,
      );
      return session;
    };

    const mesh = new Mesh(
      roomId,
      (event) => {
        if (!alive) return;
        switch (event.type) {
          case "connected":
            clearTimeout(lostTimers.get(event.peerId));
            lostTimers.delete(event.peerId);
            void (async () => {
              let session = sessions.get(event.peerId);
              const reconnect = session !== undefined;
              if (!session) {
                session = await createSession(event.peerId);
                if (!alive) return;
                sessions.set(event.peerId, session);
              }
              await session.attach(event.channel);
              if (reconnect) addLog("system", `Reconnected to ${session.peerName}.`);
              setEverConnected(true);
              setError(undefined);
              updatePeers();
              session.ping();
            })();
            break;
          case "gone":
            remove(event.peerId, "left the room.");
            break;
          case "unreachable":
            addLog("system", `Can't reach peer-${event.peerId.slice(0, 4)}: ${event.message}`);
            break;
          case "fatal":
            setError(event.message);
            break;
        }
      },
      setMeshPhase,
      password,
    );

    // Each file goes to everyone connected, in parallel (#27); interrupted
    // sends are retried after reconnecting, resuming where they stopped (#19)
    const sendWithRetry = async (session: Session, file: File) => {
      for (let attempt = 1; attempt <= MAX_SEND_ATTEMPTS; attempt++) {
        const generation = session.generation;
        const t = await session.transfers.sendFile(file, session.sendOptions());
        if (!t.interrupted || attempt === MAX_SEND_ATTEMPTS) return;
        if (!(await session.waitReady(generation, RETRY_WAIT_MS))) return;
      }
    };
    const queue = new SendQueue(async (file) => {
      const targets = [...sessions.values()].filter((s) => s.connected && s.canSendFiles());
      if (targets.length === 0) {
        addLog("system", `Nobody is connected; ${file.name} wasn't sent.`);
        return;
      }
      await Promise.all(targets.map((s) => sendWithRetry(s, file)));
    }, setQueued);
    queueRef.current = queue;

    const sayBye = () => {
      for (const s of sessions.values()) s.sayBye();
    };
    // Closing the tab is leaving on purpose, not a network failure
    addEventListener("pagehide", sayBye);

    mesh.start(abort.signal).catch((err: unknown) => {
      if (abort.signal.aborted) return;
      setError((err as Error).message);
    });
    const pinger = setInterval(() => {
      for (const s of sessions.values()) if (s.connected) s.ping();
    }, LATENCY_INTERVAL_MS);

    return () => {
      alive = false;
      removeEventListener("pagehide", sayBye);
      sayBye();
      abort.abort();
      clearInterval(pinger);
      for (const timer of lostTimers.values()) clearTimeout(timer);
      queue.clear();
      // Give the byes a moment to leave before the connections close
      setTimeout(() => {
        for (const s of sessions.values()) s.close();
        sessions.clear();
        mesh.close();
      }, 100);
    };
  }, [roomId, password, addLog]);

  const sendChat = useCallback(
    (text: string) => {
      // Group chat (#24) over the broadcast primitive (#25)
      let reached = 0;
      for (const s of sessionsRef.current.values()) {
        try {
          s.sendChat(text);
          reached++;
        } catch {
          // that connection is down
        }
      }
      addLog("me", text);
      if (reached === 0) addLog("system", "Nobody is connected; that wasn't sent.");
    },
    [addLog],
  );

  const sendFiles = useCallback((files: File[]) => queueRef.current?.add(files), []);
  const unqueue = useCallback((id: string) => queueRef.current?.remove(id), []);
  const cancel = useCallback((key: string) => {
    const [peerId, transferId] = key.split(":");
    sessionsRef.current.get(peerId!)?.transfers.cancel(transferId!);
  }, []);
  const ping = useCallback(() => {
    manualPing.current = true;
    for (const s of sessionsRef.current.values()) if (s.connected) s.ping();
  }, []);

  /** The user compared codes with a peer (#32): trust it, or cut it off. */
  const verify = useCallback(
    (peerId: string, matches: boolean) => {
      const session = sessionsRef.current.get(peerId);
      if (!session) return;
      if (matches) {
        session.markVerified();
        addLog("system", `${session.peerName} is now a verified device.`);
      } else {
        session.refuse(
          `The codes didn't match: someone may be intercepting your connection to ` +
            `${session.peerName}. Disconnected.`,
        );
      }
    },
    [addLog],
  );

  const anyConnected = peers.some((p) => p.connected);
  const phase: Phase =
    error && !everConnected
      ? "error"
      : anyConnected
        ? "connected"
        : peers.length > 0 && everConnected
          ? "reconnecting"
          : everConnected
            ? "closed"
            : meshPhase;
  const busy = queued.length > 0 || transfers.some((t) => t.status === "streaming");

  return {
    phase,
    error,
    log,
    transfers,
    queued,
    peers,
    peerReady: anyConnected,
    latencyMs: anyConnected ? latencyMs : undefined,
    busy,
    sendChat,
    sendFiles,
    unqueue,
    cancel,
    ping,
    verify,
  };
}
