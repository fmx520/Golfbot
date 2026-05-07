from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from multiprocessing import Process, Queue
from queue import Empty
from threading import Lock, Thread
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Golfbot Web UI")


class StartJobRequest(BaseModel):
    course_id: str
    play_date: str
    players: int = Field(ge=1, le=4)
    time_start: str
    time_end: str
    dry_run: bool = True


@dataclass
class JobState:
    job_id: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    events: list[dict[str, Any]] = field(default_factory=list)


JOBS: dict[str, JobState] = {}
JOB_LOCK = Lock()


def _event(job: JobState, stage: str, message: str, **extra: Any) -> None:
    job.updated_at = datetime.now(timezone.utc).isoformat()
    job.events.append({"ts": job.updated_at, "stage": stage, "message": message, **extra})


def _worker_main(job_id: str, payload: dict[str, Any], queue: Queue) -> None:
    """Timing-sensitive worker process.

    This process is intentionally isolated from web request handling so UI rendering
    cannot block booking-loop timing.
    """
    queue.put({"type": "status", "status": "running"})

    # Placeholder fast loop (replace with BookingExecutor wiring).
    poll_interval = 0.05
    timeout_seconds = 5.0
    start = time.monotonic()
    found = None

    slots = [
        {"starts_at": "07:10", "spots": 4, "price": 129.0, "tee_time_id": f"{payload['course_id']}-1"},
        {"starts_at": "07:40", "spots": 2, "price": 149.0, "tee_time_id": f"{payload['course_id']}-2"},
        {"starts_at": "08:20", "spots": 4, "price": 139.0, "tee_time_id": f"{payload['course_id']}-3"},
    ]

    while time.monotonic() - start <= timeout_seconds:
        for slot in slots:
            if slot["spots"] >= payload["players"] and payload["time_start"] <= slot["starts_at"] <= payload["time_end"]:
                found = slot
                break
        if found:
            break
        time.sleep(poll_interval)

    if not found:
        queue.put({"type": "event", "stage": "discovery", "message": "No matching tee time before timeout"})
        queue.put({"type": "status", "status": "failed"})
        return

    queue.put({"type": "event", "stage": "discovery", "message": "Found matching tee time", "slot": found})

    if payload.get("dry_run", True):
        queue.put({"type": "event", "stage": "confirmation_gate", "message": "Dry run enabled; not submitting"})
        queue.put({"type": "status", "status": "succeeded"})
        return

    queue.put({"type": "event", "stage": "confirmation_gate", "message": "Submitted booking request (simulated)", "slot": found})
    queue.put({"type": "status", "status": "succeeded"})


def _watch_worker(job_id: str, queue: Queue, process: Process) -> None:
    while process.is_alive() or not queue.empty():
        try:
            msg = queue.get(timeout=0.25)
        except Empty:
            continue

        with JOB_LOCK:
            job = JOBS[job_id]
            if msg["type"] == "status":
                job.status = msg["status"]
                job.updated_at = datetime.now(timezone.utc).isoformat()
            elif msg["type"] == "event":
                _event(job, msg["stage"], msg["message"], **{k: v for k, v in msg.items() if k not in {"type", "stage", "message"}})

    process.join(timeout=1)


@app.post("/jobs")
def start_job(req: StartJobRequest) -> dict[str, str]:
    job_id = uuid.uuid4().hex[:12]
    state = JobState(job_id=job_id)
    with JOB_LOCK:
        JOBS[job_id] = state

    payload = req.model_dump()
    queue: Queue = Queue()
    proc = Process(target=_worker_main, args=(job_id, payload, queue), daemon=True)
    proc.start()

    watcher = Thread(target=_watch_worker, args=(job_id, queue, proc), daemon=True)
    watcher.start()

    with JOB_LOCK:
        _event(JOBS[job_id], "job", "Job started", request=payload)

    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return {
            "job_id": job.job_id,
            "status": job.status,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "events": job.events,
        }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
<!doctype html><html><body>
<h2>Golfbot Web UI</h2>
<form id='f'>
  Course ID <input name='course_id' value='pebble_beach'><br/>
  Play Date <input name='play_date' value='2026-05-10'><br/>
  Players <input name='players' value='2' type='number' min='1' max='4'><br/>
  Time Start <input name='time_start' value='07:00'><br/>
  Time End <input name='time_end' value='09:00'><br/>
  Dry Run <input name='dry_run' type='checkbox' checked><br/>
  <button>Start Job</button>
</form>
<pre id='out'></pre>
<script>
const out = document.getElementById('out');
document.getElementById('f').onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const payload = Object.fromEntries(fd.entries());
  payload.players = Number(payload.players);
  payload.dry_run = fd.get('dry_run') === 'on';
  const res = await fetch('/jobs', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(payload)});
  const {job_id} = await res.json();
  out.textContent = 'job ' + job_id + '\n';

  const timer = setInterval(async () => {
    const jr = await fetch('/jobs/' + job_id);
    const js = await jr.json();
    out.textContent = JSON.stringify(js, null, 2);
    if (['succeeded','failed'].includes(js.status)) clearInterval(timer);
  }, 400);
}
</script>
</body></html>
"""
