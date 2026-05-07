from __future__ import annotations

import os
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

from src.booking.foreup_client import ForeUpBookingClient
from src.discovery.foreup_client import ForeUpDiscoveryClient, ForeUpError

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


def _emit_event(
    queue: Queue,
    *,
    phase: str,
    stage: str,
    message: str,
    terminal: str | None = None,
    retry_count: int = 0,
    latency_ms: float | None = None,
    **extra: Any,
) -> None:
    queue.put(
        {
            "type": "event",
            "provider": "foreup",
            "phase": phase,
            "stage": stage,
            "message": message,
            "retry_count": retry_count,
            "latency_ms": latency_ms,
            "terminal": terminal,
            **extra,
        }
    )


def _select_slot(slots: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any] | None:
    for slot in slots:
        starts_at = str(slot.get("starts_at") or slot.get("time") or "")[:5]
        spots = int(slot.get("spots") or slot.get("available_spots") or 0)
        if spots >= payload["players"] and payload["time_start"] <= starts_at <= payload["time_end"]:
            return slot
    return None


def _worker_main(job_id: str, payload: dict[str, Any], queue: Queue) -> None:
    queue.put({"type": "status", "status": "running"})

    base_url = os.getenv("FOREUP_BASE_URL", "http://localhost:8080")
    username = os.getenv("FOREUP_USERNAME", "")
    password = os.getenv("FOREUP_PASSWORD", "")

    discovery = ForeUpDiscoveryClient(base_url=base_url)
    booking = ForeUpBookingClient(base_url=base_url)

    request_id = f"job-{job_id}"

    try:
        auth_started = time.perf_counter()
        if username and password:
            discovery.bootstrap_session(username=username, password=password, request_id=request_id)
            _emit_event(
                queue,
                phase="auth",
                stage="session",
                message="ForeUp session bootstrapped",
                latency_ms=round((time.perf_counter() - auth_started) * 1000, 2),
            )
        else:
            _emit_event(
                queue,
                phase="auth",
                stage="session",
                message="Skipping ForeUp bootstrap (credentials not configured)",
                latency_ms=round((time.perf_counter() - auth_started) * 1000, 2),
            )

        fetch_started = time.perf_counter()
        availability = discovery.lookup_availability(
            course_id=payload["course_id"], date=payload["play_date"], players=payload["players"], request_id=request_id
        )
        _emit_event(
            queue,
            phase="discovery",
            stage="course_availability_fetch",
            message="Fetched course availability",
            latency_ms=round((time.perf_counter() - fetch_started) * 1000, 2),
        )

        slots = availability.get("slots") if isinstance(availability, dict) else None
        if not isinstance(slots, list):
            slots = []

        filter_started = time.perf_counter()
        selected = _select_slot(slots, payload)
        _emit_event(
            queue,
            phase="discovery",
            stage="tee_time_filtering",
            message="Filtered tee times for requested window",
            latency_ms=round((time.perf_counter() - filter_started) * 1000, 2),
            candidates=len(slots),
        )

        if not selected:
            _emit_event(
                queue,
                phase="confirm",
                stage="terminal",
                message="No matching tee times found",
                terminal="failed",
            )
            queue.put({"type": "status", "status": "failed"})
            return

        if payload.get("dry_run", True):
            _emit_event(
                queue,
                phase="confirm",
                stage="booking_confirmation",
                message="Dry run enabled; booking submit skipped",
                terminal="dry_run_stopped",
                slot=selected,
            )
            queue.put({"type": "status", "status": "succeeded"})
            return

        submit_started = time.perf_counter()
        booking_payload = {
            "course_id": payload["course_id"],
            "date": payload["play_date"],
            "players": payload["players"],
            "tee_time_id": selected.get("tee_time_id"),
        }
        confirm_payload = dict(booking_payload)
        booking_response, booking_latency = booking.book_slot(
            idempotency_key=f"job-{job_id}-{selected.get('tee_time_id', 'unknown')}",
            slot_payload=booking_payload,
            confirm_payload=confirm_payload,
            request_id=request_id,
        )
        _emit_event(
            queue,
            phase="checkout",
            stage="booking_submit",
            message="Submitted booking request",
            latency_ms=round((time.perf_counter() - submit_started) * 1000, 2),
            provider_latency_ms=booking_latency.total_ms,
        )
        _emit_event(
            queue,
            phase="confirm",
            stage="booking_confirmation",
            message="Booking confirmed",
            terminal="success",
            confirmation=booking_response,
        )
        queue.put({"type": "status", "status": "succeeded"})
    except ForeUpError as exc:
        _emit_event(queue, phase="confirm", stage="terminal", message=f"ForeUp request failed: {exc}", terminal="failed")
        queue.put({"type": "status", "status": "failed"})
    except Exception as exc:  # noqa: BLE001
        _emit_event(queue, phase="confirm", stage="terminal", message=f"Unexpected worker error: {exc}", terminal="failed")
        queue.put({"type": "status", "status": "failed"})


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
