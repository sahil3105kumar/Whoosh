import { useEffect, useState } from "react";

import { Home } from "./components/Home";
import { Room } from "./components/Room";

type Screen =
  | { name: "home" }
  // attempt: bumped by "Try again" so the room remounts with a fresh connection
  | { name: "room"; roomId: string; attempt?: number; password?: string };

/** A shared link looks like https://host/#abcd1234 and opens straight into the room. */
function roomFromHash(): string | undefined {
  const id = location.hash.slice(1);
  return /^[0-9a-f]{8}$/.test(id) ? id : undefined;
}

export default function App() {
  const [screen, setScreen] = useState<Screen>(() => {
    const roomId = roomFromHash();
    return roomId ? { name: "room", roomId } : { name: "home" };
  });

  useEffect(() => {
    const onHash = () => {
      const roomId = roomFromHash();
      if (roomId) setScreen({ name: "room", roomId });
    };
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);

  const leave = () => {
    history.replaceState(null, "", location.pathname);
    setScreen({ name: "home" });
  };

  return (
    <div className="mx-auto flex min-h-full max-w-4xl flex-col px-4 py-6 sm:py-10">
      <header className="mb-8 flex items-center gap-2">
        <span className="text-2xl" aria-hidden>
          💨
        </span>
        <h1 className="text-xl font-semibold tracking-tight">Whoosh</h1>
        <span className="ml-2 text-sm text-slate-500 dark:text-slate-400">
          peer-to-peer chat &amp; file transfer
        </span>
      </header>

      <main className="flex-1">
        {screen.name === "home" ? (
          <Home
            onCreated={(roomId, password) => setScreen({ name: "room", roomId, password })}
            onJoin={(roomId) => setScreen({ name: "room", roomId })}
          />
        ) : (
          // key: a new room id (or a retry) gets a fresh connection
          <Room
            key={`${screen.roomId}:${screen.attempt ?? 0}`}
            roomId={screen.roomId}
            password={screen.password}
            onLeave={leave}
            // A retry asks for the password again (it may have been wrong)
            onRetry={() =>
              setScreen({ ...screen, password: undefined, attempt: (screen.attempt ?? 0) + 1 })
            }
          />
        )}
      </main>

      <footer className="mt-10 text-center text-xs text-slate-500 dark:text-slate-400">
        Files and messages go directly between browsers. The server only introduces you.
      </footer>
    </div>
  );
}
