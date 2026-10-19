import { type FormEvent, useEffect, useRef, useState } from "react";

import type { QueuedFile } from "../lib/queue";
import type { Transfer } from "../lib/transfer";
import { type LogEntry, type Phase, useRoom } from "../useRoom";
import { Button, Card, formatBytes } from "./ui";

const phaseText: Record<Phase, string> = {
  signaling: "Contacting server…",
  waiting: "Waiting for the other person to join…",
  connecting: "Connecting directly…",
  connected: "Connected peer-to-peer",
  closed: "The other person left",
  error: "Couldn't connect",
};

export function Room({
  roomId,
  isCreator,
  onLeave,
  onRetry,
}: {
  roomId: string;
  isCreator: boolean;
  onLeave: () => void;
  onRetry: () => void;
}) {
  const room = useRoom(roomId, isCreator);
  const live = room.phase === "connected";
  const canSend = live && room.peerReady;
  const dragging = usePageDrop(canSend, room.sendFiles);
  useUnreadTitle(room.log);
  useWarnBeforeUnload(room.busy);

  const leave = () => {
    if (!room.busy || confirm("Files are still being transferred. Leave anyway?")) onLeave();
  };

  return (
    <div className="flex flex-col gap-4">
      {dragging && (
        <div className="pointer-events-none fixed inset-0 z-10 flex items-center justify-center bg-sky-500/15 backdrop-blur-[1px]">
          <p className="rounded-xl bg-white px-6 py-4 text-lg font-medium shadow-lg dark:bg-slate-900">
            Drop to send
          </p>
        </div>
      )}

      <Card className="flex flex-wrap items-center gap-3">
        <StatusDot phase={room.phase} />
        <div className="min-w-0 flex-1">
          <p className="font-medium">{phaseText[room.phase]}</p>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Room <span className="font-mono">{roomId}</span>
            {live && room.latencyMs !== undefined && (
              <span title="Round-trip time to the other person">
                {" "}
                · {room.latencyMs < 1 ? "<1" : room.latencyMs.toFixed(0)} ms
              </span>
            )}
          </p>
        </div>
        {live && (
          <Button variant="ghost" onClick={room.ping}>
            Ping
          </Button>
        )}
        {room.phase === "error" && <Button onClick={onRetry}>Try again</Button>}
        <Button variant="secondary" onClick={leave}>
          Leave
        </Button>
        {room.error && (
          <p className="w-full text-sm text-red-600 dark:text-red-400" role="alert">
            {room.error}
          </p>
        )}
      </Card>

      {room.phase === "waiting" && isCreator && <ShareBox roomId={roomId} />}

      {(live || room.log.length > 0 || room.transfers.length > 0) && (
        <div className="grid gap-4 md:grid-cols-2">
          <Chat log={room.log} onSend={room.sendChat} disabled={!live} />
          <Files
            transfers={room.transfers}
            queued={room.queued}
            canSend={canSend}
            onSend={room.sendFiles}
            onCancel={room.cancel}
            onUnqueue={room.unqueue}
          />
        </div>
      )}
    </div>
  );
}

/** Accept files dropped anywhere on the page; returns whether a drag is over it. */
function usePageDrop(enabled: boolean, onFiles: (files: File[]) => void): boolean {
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    // dragenter/dragleave fire for every child element crossed; count them
    let depth = 0;
    const hasFiles = (e: DragEvent) => e.dataTransfer?.types.includes("Files") ?? false;
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth++;
      setDragging(true);
    };
    const leave = () => {
      depth = Math.max(0, depth - 1);
      if (depth === 0) setDragging(false);
    };
    const over = (e: DragEvent) => {
      if (hasFiles(e)) e.preventDefault(); // allow dropping
    };
    const drop = (e: DragEvent) => {
      e.preventDefault(); // don't let the browser open the file
      depth = 0;
      setDragging(false);
      const files = [...(e.dataTransfer?.files ?? [])];
      if (files.length) onFiles(files);
    };
    addEventListener("dragenter", enter);
    addEventListener("dragleave", leave);
    addEventListener("dragover", over);
    addEventListener("drop", drop);
    return () => {
      setDragging(false);
      removeEventListener("dragenter", enter);
      removeEventListener("dragleave", leave);
      removeEventListener("dragover", over);
      removeEventListener("drop", drop);
    };
  }, [enabled, onFiles]);

  return dragging;
}

/** Count chat messages that arrive while the tab is hidden, in its title. */
function useUnreadTitle(log: LogEntry[]): void {
  const [unread, setUnread] = useState(0);
  const seen = useRef(log.length);

  useEffect(() => {
    const fresh = log.slice(seen.current).filter((e) => e.from === "peer").length;
    seen.current = log.length;
    if (fresh && document.hidden) setUnread((n) => n + fresh);
  }, [log]);

  useEffect(() => {
    const onVisible = () => {
      if (!document.hidden) setUnread(0);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, []);

  useEffect(() => {
    document.title = unread ? `(${unread}) Whoosh` : "Whoosh";
    return () => {
      document.title = "Whoosh";
    };
  }, [unread]);
}

function useWarnBeforeUnload(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    addEventListener("beforeunload", warn);
    return () => removeEventListener("beforeunload", warn);
  }, [active]);
}

function StatusDot({ phase }: { phase: Phase }) {
  const color =
    phase === "connected"
      ? "bg-emerald-500"
      : phase === "error" || phase === "closed"
        ? "bg-red-500"
        : "animate-pulse bg-amber-400";
  return <span className={`size-3 shrink-0 rounded-full ${color}`} aria-hidden />;
}

function ShareBox({ roomId }: { roomId: string }) {
  const link = `${location.origin}${location.pathname}#${roomId}`;
  const [copied, setCopied] = useState<string>();

  const copy = async (what: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(what);
      setTimeout(() => setCopied(undefined), 1500);
    } catch {
      // clipboard blocked (e.g. insecure context): the text is selectable anyway
    }
  };

  return (
    <Card>
      <h2 className="font-medium">Invite someone</h2>
      <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
        Send them this link, or the code to use with{" "}
        <code className="font-mono">whoosh join {roomId}</code>.
      </p>
      <div className="mt-4 flex flex-col gap-2 sm:flex-row">
        <input
          readOnly
          value={link}
          onFocus={(e) => e.target.select()}
          className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-slate-50 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
          aria-label="Invite link"
        />
        <Button onClick={() => copy("link", link)}>{copied === "link" ? "Copied" : "Copy link"}</Button>
        <Button variant="secondary" onClick={() => copy("code", roomId)}>
          {copied === "code" ? "Copied" : "Copy code"}
        </Button>
      </div>
    </Card>
  );
}

function Chat({
  log,
  onSend,
  disabled,
}: {
  log: LogEntry[];
  onSend: (text: string) => void;
  disabled: boolean;
}) {
  const [text, setText] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  // Block body: newer browsers return a Promise from scrollIntoView, and an
  // effect must return nothing or a cleanup function.
  useEffect(() => {
    void bottom.current?.scrollIntoView({ block: "nearest" });
  }, [log]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = text.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setText("");
  };

  return (
    <Card className="flex h-96 flex-col">
      <h2 className="font-medium">Chat</h2>
      <div className="mt-3 flex-1 space-y-2 overflow-y-auto pr-1" aria-live="polite">
        {log.length === 0 && (
          <p className="text-sm text-slate-500 dark:text-slate-400">No messages yet.</p>
        )}
        {log.map((entry) =>
          entry.from === "system" ? (
            <p key={entry.id} className="text-center text-xs text-slate-500 dark:text-slate-400">
              {entry.text}
            </p>
          ) : (
            <div key={entry.id} className={`flex ${entry.from === "me" ? "justify-end" : ""}`}>
              <p
                className={`max-w-[85%] rounded-2xl px-3 py-1.5 text-sm break-words whitespace-pre-wrap ${
                  entry.from === "me"
                    ? "bg-sky-600 text-white"
                    : "bg-slate-100 dark:bg-slate-800"
                }`}
              >
                {entry.text}
              </p>
            </div>
          ),
        )}
        <div ref={bottom} />
      </div>
      <form className="mt-3 flex gap-2" onSubmit={submit}>
        <input
          className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-500/30 disabled:opacity-60 dark:border-slate-700 dark:bg-slate-900"
          placeholder={disabled ? "Not connected" : "Message"}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={disabled}
          aria-label="Message"
        />
        <Button type="submit" disabled={disabled || !text.trim()}>
          Send
        </Button>
      </form>
    </Card>
  );
}

function Files({
  transfers,
  queued,
  canSend,
  onSend,
  onCancel,
  onUnqueue,
}: {
  transfers: Transfer[];
  queued: readonly QueuedFile[];
  canSend: boolean;
  onSend: (files: File[]) => void;
  onCancel: (id: string) => void;
  onUnqueue: (id: string) => void;
}) {
  const input = useRef<HTMLInputElement>(null);

  return (
    <Card className="flex h-96 flex-col">
      <h2 className="font-medium">Files</h2>
      {/* Drops anywhere on the page are handled by usePageDrop */}
      <div className="mt-3 rounded-lg border-2 border-dashed border-slate-300 p-4 text-center text-sm dark:border-slate-700">
        <p className="text-slate-600 dark:text-slate-400">
          {canSend ? "Drop files here or" : "Waiting for the connection…"}
        </p>
        {canSend && (
          <Button variant="secondary" className="mt-2" onClick={() => input.current?.click()}>
            Choose files
          </Button>
        )}
        <input
          ref={input}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            if (e.target.files) onSend([...e.target.files]);
            e.target.value = "";
          }}
        />
      </div>

      <ul className="mt-3 flex-1 space-y-2 overflow-y-auto pr-1">
        {queued.map((q, i) => (
          <li
            key={q.id}
            className="flex items-center gap-2 rounded-lg border border-dashed border-slate-300 p-3 text-sm dark:border-slate-700"
          >
            <span aria-hidden>⏳</span>
            <span className="min-w-0 flex-1 truncate" title={q.file.name}>
              {q.file.name}
            </span>
            <span className="shrink-0 text-xs text-slate-500 dark:text-slate-400">
              Queued #{i + 1} · {formatBytes(q.file.size)}
            </span>
            <button
              className="text-xs text-slate-500 hover:text-red-600 dark:text-slate-400"
              onClick={() => onUnqueue(q.id)}
            >
              Remove
            </button>
          </li>
        ))}
        {[...transfers].reverse().map((t) => (
          <TransferRow key={t.id} transfer={t} onCancel={() => onCancel(t.id)} />
        ))}
        {queued.length === 0 && transfers.length === 0 && (
          <li className="text-sm text-slate-500 dark:text-slate-400">No files yet.</li>
        )}
      </ul>
    </Card>
  );
}

function TransferRow({ transfer: t, onCancel }: { transfer: Transfer; onCancel: () => void }) {
  const pct = t.size ? (100 * t.bytesDone) / t.size : 100;
  const seconds = (performance.now() - t.startedAt) / 1000;
  // Speed counts this session's bytes only, not what a resume skipped
  const moved = t.bytesDone - t.resumedFrom;
  const speed = seconds > 0.5 ? moved / seconds : 0;
  const rate = speed ? `${formatBytes(speed)}/s` : "";
  const eta = speed ? formatDuration((t.size - t.bytesDone) / speed) : "";
  const resumed = t.resumedFrom
    ? `resumed from ${((100 * t.resumedFrom) / (t.size || 1)).toFixed(0)}%`
    : "";
  const arrow = t.direction === "send" ? "↑" : "↓";

  return (
    <li className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-800">
      <div className="flex items-center gap-2">
        <span aria-label={t.direction === "send" ? "Sending" : "Receiving"}>{arrow}</span>
        <span className="min-w-0 flex-1 truncate font-medium" title={t.filename}>
          {t.filename}
        </span>
        <span className="shrink-0 text-xs text-slate-500 dark:text-slate-400">
          {formatBytes(t.size)}
        </span>
      </div>

      {t.status === "streaming" && (
        <>
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
            <div className="h-full bg-sky-500 transition-[width]" style={{ width: `${pct}%` }} />
          </div>
          <div className="mt-1 flex items-center text-xs text-slate-500 dark:text-slate-400">
            <span className="flex-1">
              {pct.toFixed(0)}% {rate && `· ${rate}`} {eta && `· ${eta} left`}{" "}
              {resumed && `· ${resumed}`}
            </span>
            <button className="hover:text-red-600" onClick={onCancel}>
              Cancel
            </button>
          </div>
        </>
      )}

      {t.status === "completed" && (
        <div className="mt-1 flex items-center text-xs">
          <span className="flex-1 text-emerald-600 dark:text-emerald-400">
            {t.direction === "send" ? "Delivered" : "Received · verified"}
            {resumed && <span className="text-slate-500 dark:text-slate-400"> · {resumed}</span>}
          </span>
          {t.url && (
            <a
              href={t.url}
              download={t.filename}
              className="rounded-md bg-sky-600 px-2.5 py-1 font-medium text-white hover:bg-sky-500"
            >
              Save
            </a>
          )}
        </div>
      )}

      {(t.status === "cancelled" || t.status === "failed") && (
        <p className="mt-1 text-xs text-red-600 dark:text-red-400">
          {t.status === "cancelled" ? "Cancelled" : "Failed"}
          {t.reason && `: ${t.reason}`}
        </p>
      )}
    </li>
  );
}

function formatDuration(seconds: number): string {
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${Math.ceil(seconds)}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ${Math.ceil(seconds % 60)}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
