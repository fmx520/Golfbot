"use client";

import { useEffect, useMemo, useState } from "react";
import { Button, Card, Input } from "@/components/ui";

type JobStatus = "queued" | "running" | "succeeded" | "failed";

type JobEvent = {
  ts: string;
  phase: string;
  message: string;
  latency_ms?: number;
  terminal?: string;
  slot?: {
    starts_at: string;
    spots: number;
    price: number;
    tee_time_id: string;
  };
};

type JobResponse = {
  job_id: string;
  status: JobStatus;
  events: JobEvent[];
};

const API_BASE = process.env.NEXT_PUBLIC_GOLFBOT_API_BASE ?? "http://localhost:8000";

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

export default function HomePage() {
  const [courseId, setCourseId] = useState("pebble_beach");
  const [courseOptions, setCourseOptions] = useState<string[]>([]);
  const [playDate, setPlayDate] = useState(todayIso());
  const [players, setPlayers] = useState(2);
  const [timeStart, setTimeStart] = useState("07:00");
  const [timeEnd, setTimeEnd] = useState("09:00");
  const [dryRun, setDryRun] = useState(true);

  const [status, setStatus] = useState<JobStatus | "idle">("idle");
  const [jobId, setJobId] = useState<string | null>(null);
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [error, setError] = useState<string | null>(null);

  const prettyEvents = useMemo(() => {
    return events.map((event) => {
      const slotSuffix = event.slot
        ? ` | ${event.slot.starts_at} (${event.slot.spots} spots) $${event.slot.price}`
        : "";
      const meta = event.latency_ms ? ` (${event.latency_ms}ms)` : "";
      return `${event.ts} [${event.phase}] ${event.message}${meta}${slotSuffix}`;
    });
  }, [events]);


  async function loadCourses() {
    try {
      const response = await fetch(`${API_BASE}/courses`, { cache: "no-store" });
      if (!response.ok) return;
      const payload = (await response.json()) as { courses: { course_id: string }[] };
      setCourseOptions(payload.courses.map((course) => course.course_id));
    } catch {
      // Non-fatal for manual course entry
    }
  }

  async function pollJob(id: string) {
    while (true) {
      const response = await fetch(`${API_BASE}/jobs/${id}`, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`Job fetch failed with ${response.status}`);
      }

      const job = (await response.json()) as JobResponse;
      setStatus(job.status);
      setEvents(job.events);

      if (job.status === "succeeded" || job.status === "failed") {
        break;
      }

      await new Promise((resolve) => setTimeout(resolve, 400));
    }
  }

  async function streamJob(id: string) {
    await new Promise<void>((resolve, reject) => {
      const stream = new EventSource(`${API_BASE}/jobs/${id}/stream`);

      stream.addEventListener("message", (event) => {
        const payload = JSON.parse((event as MessageEvent).data) as { status: JobStatus; event: JobEvent };
        setStatus(payload.status);
        setEvents((prev) => [...prev, payload.event]);
      });

      stream.addEventListener("done", () => {
        stream.close();
        resolve();
      });

      stream.addEventListener("error", () => {
        stream.close();
        reject(new Error("Stream disconnected"));
      });
    });
  }

  async function runBot() {
    setError(null);
    setEvents([]);
    setStatus("queued");
    setJobId(null);

    try {
      const response = await fetch(`${API_BASE}/jobs`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          course_id: courseId,
          play_date: playDate,
          players,
          time_start: timeStart,
          time_end: timeEnd,
          dry_run: dryRun,
        }),
      });

      if (!response.ok) {
        const payload = await response.text();
        throw new Error(`Start job failed (${response.status}): ${payload}`);
      }

      const payload = (await response.json()) as { job_id: string };
      setJobId(payload.job_id);
      try {
        await streamJob(payload.job_id);
      } catch {
        await pollJob(payload.job_id);
      }
    } catch (runError) {
      setStatus("failed");
      setError(runError instanceof Error ? runError.message : "Unexpected error");
    }
  }

  useEffect(() => {
    void loadCourses();
  }, []);

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-3xl flex-col gap-4 p-6">
      <h1 className="text-2xl font-semibold">Golfbot</h1>

      <Card className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-sm font-medium text-slate-700">Course ID</label>
            <Input list="courses" value={courseId} onChange={(event) => setCourseId(event.target.value)} />
            <datalist id="courses">{courseOptions.map((course) => <option key={course} value={course} />)}</datalist>
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-slate-700">Play Date</label>
            <Input type="date" value={playDate} onChange={(event) => setPlayDate(event.target.value)} />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-slate-700">Players</label>
            <Input type="number" min={1} max={4} value={players} onChange={(event) => setPlayers(Number(event.target.value))} />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-slate-700">Time Window</label>
            <div className="flex items-center gap-2">
              <Input type="time" value={timeStart} onChange={(event) => setTimeStart(event.target.value)} />
              <span className="text-slate-500">to</span>
              <Input type="time" value={timeEnd} onChange={(event) => setTimeEnd(event.target.value)} />
            </div>
          </div>
        </div>

        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input type="checkbox" checked={dryRun} onChange={(event) => setDryRun(event.target.checked)} />
          Dry run (safe mode)
        </label>

        <Button disabled={status === "running" || status === "queued"} onClick={() => void runBot()}>
          {status === "running" || status === "queued" ? "Running…" : "Run"}
        </Button>
      </Card>

      <Card>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-medium text-slate-700">Output</h2>
          <span className="rounded-full border border-slate-300 px-2 py-0.5 text-xs uppercase text-slate-600">{status}</span>
        </div>
        {jobId ? <p className="mb-2 text-xs text-slate-500">Job ID: {jobId}</p> : null}
        {error ? <p className="mb-2 text-xs text-red-600">{error}</p> : null}

        <div className="h-72 overflow-auto rounded-md bg-slate-950 p-3 font-mono text-xs text-slate-100">
          {prettyEvents.length === 0 ? (
            <p className="text-slate-400">No runs yet.</p>
          ) : (
            prettyEvents.map((line) => <p key={line}>{line}</p>)
          )}
        </div>
      </Card>
    </main>
  );
}
