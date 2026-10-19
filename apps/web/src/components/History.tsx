import { useState } from "react";

import { type HistoryEntry, clearHistory, loadHistory } from "../lib/history";
import { Card, formatBytes } from "./ui";

/** Past transfers kept in this browser (#22). */
export function History() {
  const [entries, setEntries] = useState<HistoryEntry[]>(() => loadHistory());
  if (entries.length === 0) return null;

  const clear = () => {
    clearHistory();
    setEntries([]);
  };

  return (
    <Card className="mt-4">
      <div className="flex items-center">
        <h2 className="flex-1 font-medium">Recent transfers</h2>
        <button
          className="text-xs text-slate-500 hover:text-red-600 dark:text-slate-400"
          onClick={clear}
        >
          Clear
        </button>
      </div>
      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
        Kept only in this browser. Received files aren't stored here; save them when they arrive.
      </p>
      <ul className="mt-3 divide-y divide-slate-200 text-sm dark:divide-slate-800">
        {entries.slice(0, 20).map((e, i) => (
          <li key={`${e.time}-${i}`} className="flex items-center gap-2 py-2">
            <span aria-label={e.direction === "send" ? "Sent" : "Received"}>
              {e.direction === "send" ? "↑" : "↓"}
            </span>
            <span className="min-w-0 flex-1 truncate" title={e.filename}>
              {e.filename}
            </span>
            <span className="shrink-0 text-xs text-slate-500 dark:text-slate-400">
              {formatBytes(e.size)}
            </span>
            <span
              className={`shrink-0 text-xs ${
                e.status === "completed"
                  ? "text-emerald-600 dark:text-emerald-400"
                  : "text-red-600 dark:text-red-400"
              }`}
              title={e.reason || undefined}
            >
              {e.status === "completed" ? "done" : e.status}
            </span>
            <time
              className="w-28 shrink-0 text-right text-xs text-slate-500 dark:text-slate-400"
              dateTime={e.time}
            >
              {new Date(e.time).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" })}
            </time>
          </li>
        ))}
      </ul>
    </Card>
  );
}
