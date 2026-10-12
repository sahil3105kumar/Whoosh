import { type FormEvent, useState } from "react";

import { createRoom } from "../lib/connection";
import { Button, Card } from "./ui";

export function Home({
  onCreated,
  onJoin,
}: {
  onCreated: (roomId: string) => void;
  onJoin: (roomId: string) => void;
}) {
  const [code, setCode] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string>();

  const valid = /^[0-9a-f]{8}$/.test(code);

  const create = async () => {
    setCreating(true);
    setError(undefined);
    try {
      onCreated(await createRoom());
    } catch (err) {
      setError((err as Error).message);
      setCreating(false);
    }
  };

  const join = (event: FormEvent) => {
    event.preventDefault();
    if (valid) onJoin(code);
  };

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Card>
        <h2 className="text-lg font-medium">Start a room</h2>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          Get a code to share with the person you want to connect with.
        </p>
        <Button className="mt-5 w-full" onClick={create} disabled={creating}>
          {creating ? "Creating…" : "Create room"}
        </Button>
        {error && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{error}</p>}
      </Card>

      <Card>
        <h2 className="text-lg font-medium">Join a room</h2>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          Enter the 8-character code you were given.
        </p>
        <form className="mt-5 flex gap-2" onSubmit={join}>
          <input
            className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-3 py-2 font-mono tracking-widest outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-500/30 dark:border-slate-700 dark:bg-slate-900"
            placeholder="a1b2c3d4"
            value={code}
            onChange={(e) => setCode(e.target.value.trim().toLowerCase())}
            maxLength={8}
            autoComplete="off"
            spellCheck={false}
            aria-label="Room code"
          />
          <Button type="submit" disabled={!valid}>
            Join
          </Button>
        </form>
      </Card>
    </div>
  );
}
