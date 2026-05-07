from __future__ import annotations

import json
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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from src.config import load_config

app = FastAPI(title="Golfbot Web UI")
_cors_origins = os.getenv("GOLFBOT_CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in _cors_origins if origin.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


def _event(job: JobState, phase: str, message: str, **extra: Any) -> None:
    job.updated_at = datetime.now(timezone.utc).isoformat()
    event = {
        "ts": job.updated_at,
        "phase": phase,
        "message": message,
        "status": job.status,
        **extra,
    }
    job.events.append(event)


def _worker_main(job_id: str, payload: dict[str, Any], queue: Queue) -> None:
    queue.put({"type": "status", "status": "running"})
    start = time.monotonic()

    queue.put({"type": "event", "phase": "auth", "message": "Session ready", "latency_ms": 0})

    poll_interval = 0.05
    timeout_seconds = 5.0
    found = None

    slots = [
        {"starts_at": "07:10", "spots": 4, "price": 129.0, "tee_time_id": f"{payload['course_id']}-1"},
        {"starts_at": "07:40", "spots": 2, "price": 149.0, "tee_time_id": f"{payload['course_id']}-2"},
        {"starts_at": "08:20", "spots": 4, "price": 139.0, "tee_time_id": f"{payload['course_id']}-3"},
    ]

    queue.put({"type": "event", "phase": "discovery", "message": "Polling tee times"})
    while time.monotonic() - start <= timeout_seconds:
        for slot in slots:
            if slot["spots"] >= payload["players"] and payload["time_start"] <= slot["starts_at"] <= payload["time_end"]:
                found = slot
                break
        if found:
            break
        time.sleep(poll_interval)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    if not found:
        queue.put({"type": "event", "phase": "discovery", "message": "No matching tee time before timeout", "latency_ms": elapsed_ms})
        queue.put({"type": "status", "status": "failed"})
        return

    queue.put({"type": "event", "phase": "discovery", "message": "Found matching tee time", "slot": found, "latency_ms": elapsed_ms})

    if payload.get("dry_run", True):
        queue.put({"type": "event", "phase": "confirmation_gate", "message": "Dry run enabled; not submitting", "terminal": "dry_run_stopped"})
        queue.put({"type": "status", "status": "succeeded"})
        return

    queue.put({"type": "event", "phase": "booking", "message": "Submitted booking request (simulated)", "slot": found})
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
                _event(job, msg["phase"], msg["message"], **{k: v for k, v in msg.items() if k not in {"type", "phase", "message"}})

    process.join(timeout=1)


@app.get("/courses")
def list_courses() -> dict[str, Any]:
    config = load_config()
    return {
        "courses": [
            {"course_id": course_id, "player_count_default": settings.player_count}
            for course_id, settings in sorted(config.courses.items())
        ]
    }


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


@app.get("/jobs/{job_id}/stream")
def stream_job(job_id: str) -> StreamingResponse:
    def event_stream():
        sent = 0
        while True:
            with JOB_LOCK:
                job = JOBS.get(job_id)
                if not job:
                    yield "event: error\ndata: Job not found\n\n"
                    return

                while sent < len(job.events):
                    event = job.events[sent]
                    payload = {"index": sent, "status": job.status, "event": event}
                    yield f"event: message\ndata: {json.dumps(payload)}\n\n"
                    sent += 1

                done = job.status in {"succeeded", "failed"}

            if done:
                yield "event: done\ndata: complete\n\n"
                return

            time.sleep(0.25)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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
<p>Use the Next.js frontend at localhost:3000 for the main interface.</p>
</body></html>
"""
