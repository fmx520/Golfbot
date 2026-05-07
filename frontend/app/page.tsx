"use client";

import { useState } from "react";
import { Button, Card, Input } from "@/components/ui";

type RunState = "idle" | "running" | "done" | "error";

export default function HomePage() {
  const [query, setQuery] = useState("");
  const [state, setState] = useState<RunState>("idle");
  const [events, setEvents] = useState<string[]>([]);

  async function runBot() {
    if (!query.trim()) {
      return;
    }

    setState("running");
    setEvents(["Starting run..."]);

    try {
      // Placeholder stream simulation until backend run API is wired.
      await new Promise((resolve) => setTimeout(resolve, 250));
      setEvents((prev) => [...prev, `Accepted query: ${query}`]);
      await new Promise((resolve) => setTimeout(resolve, 250));
      setEvents((prev) => [...prev, "Awaiting backend stream integration..."]);
      setState("done");
    } catch {
      setState("error");
      setEvents((prev) => [...prev, "Run failed"]);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-3xl flex-col gap-4 p-6">
      <h1 className="text-2xl font-semibold">Golfbot</h1>

      <Card className="space-y-3">
        <label htmlFor="query" className="text-sm font-medium text-zinc-700">
          Query
        </label>
        <div className="flex gap-2">
          <Input
            id="query"
            placeholder="Find earliest tee time at Presidio tomorrow"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                void runBot();
              }
            }}
          />
          <Button disabled={state === "running"} onClick={() => void runBot()}>
            {state === "running" ? "Running…" : "Run"}
          </Button>
        </div>
      </Card>

      <Card>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-medium text-zinc-700">Output</h2>
          <span className="rounded-full border border-zinc-300 px-2 py-0.5 text-xs uppercase text-zinc-600">{state}</span>
        </div>
        <div className="h-64 overflow-auto rounded-md bg-zinc-950 p-3 font-mono text-xs text-zinc-100">
          {events.length === 0 ? <p className="text-zinc-400">No runs yet.</p> : events.map((line) => <p key={line}>{line}</p>)}
        </div>
      </Card>
    </main>
  );
}
