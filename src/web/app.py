from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from multiprocessing import Process, Queue
from pathlib import Path
from queue import Empty
from threading import Lock, Thread
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.booking.foreup_client import ForeUpBookingClient
from src.config import load_config
from src.discovery.foreup_client import AntiBotDetectedError, ForeUpDiscoveryClient, ForeUpError

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"

app = FastAPI(title="Golfbot Web UI")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ConnectRequest(BaseModel):
    username: str = ""
    password: str = ""
    booking_url: str = ""
    schedule_id: str = ""
    course_id: str = ""


class StartJobRequest(BaseModel):
    course_id: str = ""
    schedule_id: str = ""
    play_date: str
    players: int = Field(ge=1, le=4)
    time_start: str
    time_end: str
    holes: int = Field(default=18, ge=9, le=18)
    dry_run: bool = True
    foreup_booking_url: str | None = None
    username: str = ""
    password: str = ""


@dataclass
class JobState:
    job_id: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    events: list[dict[str, Any]] = field(default_factory=list)


JOBS: dict[str, JobState] = {}
JOB_LOCK = Lock()
APP_CONFIG = load_config()


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
        slot_time = slot.get("time", "")[:5]
        spots = int(slot.get("available_spots", slot.get("spots", 0)))
        if spots >= payload["players"] and payload["time_start"] <= slot_time <= payload["time_end"]:
            return slot
    return None


def _extract_foreup_target(booking_url: str | None, fallback_base_url: str, fallback_schedule_id: str, fallback_course_id: str) -> tuple[str, str, str]:
    if not booking_url:
        return fallback_base_url, fallback_schedule_id, fallback_course_id

    parsed = urlparse(booking_url)
    if not parsed.scheme or not parsed.netloc:
        return fallback_base_url, fallback_schedule_id, fallback_course_id

    path_parts = [part for part in parsed.path.split("/") if part]
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    schedule_id = fallback_schedule_id
    course_id = fallback_course_id

    if "booking" in path_parts:
        booking_ix = path_parts.index("booking")
        remaining = path_parts[booking_ix + 1:]
        if len(remaining) >= 1:
            schedule_id = remaining[0]
        if len(remaining) >= 2:
            course_id = remaining[1]

    return base_url, schedule_id, course_id


def _worker_main(job_id: str, payload: dict[str, Any], queue: Queue) -> None:
    queue.put({"type": "status", "status": "running"})

    def _finalize_worker() -> None:
        queue.put({"type": "worker_done"})

    default_base_url = os.getenv("FOREUP_BASE_URL", "https://foreupsoftware.com")
    default_schedule_id = os.getenv("FOREUP_SCHEDULE_ID", "")
    default_course_id = os.getenv("FOREUP_COURSE_ID", "")

    base_url, resolved_schedule_id, resolved_course_id = _extract_foreup_target(
        payload.get("foreup_booking_url"),
        fallback_base_url=default_base_url,
        fallback_schedule_id=payload.get("schedule_id") or default_schedule_id,
        fallback_course_id=payload.get("course_id") or default_course_id,
    )

    username = payload.get("username") or os.getenv("FOREUP_USERNAME", "")
    password = payload.get("password") or os.getenv("FOREUP_PASSWORD", "")
    holes = payload.get("holes", 18)

    if not resolved_schedule_id:
        _emit_event(
            queue,
            phase="confirm",
            stage="terminal",
            message="No schedule_id provided. Enter a ForeUp booking URL or set FOREUP_SCHEDULE_ID.",
            terminal="failed",
        )
        queue.put({"type": "status", "status": "failed"})
        _finalize_worker()
        return

    discovery = ForeUpDiscoveryClient(base_url=base_url)
    booking = ForeUpBookingClient(base_url=base_url)

    request_id = f"job-{job_id}"

    try:
        auth_started = time.perf_counter()
        if username and password:
            discovery.bootstrap_session(username=username, password=password, schedule_id=resolved_schedule_id, request_id=request_id)
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
                message="No credentials provided — attempting unauthenticated lookup",
                latency_ms=round((time.perf_counter() - auth_started) * 1000, 2),
            )

        fetch_started = time.perf_counter()
        slots = discovery.lookup_availability(
            schedule_id=resolved_schedule_id,
            date=payload["play_date"],
            players=payload["players"],
            holes=holes,
            request_id=request_id,
        )
        _emit_event(
            queue,
            phase="discovery",
            stage="course_availability_fetch",
            message=f"Fetched {len(slots)} available tee times",
            latency_ms=round((time.perf_counter() - fetch_started) * 1000, 2),
        )

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
                message="No matching tee times found in the requested window",
                terminal="failed",
            )
            queue.put({"type": "status", "status": "failed"})
            _finalize_worker()
            return

        if payload.get("dry_run", True):
            _emit_event(
                queue,
                phase="confirm",
                stage="booking_confirmation",
                message=f"Dry run — found slot at {selected['time']} with {selected['available_spots']} spots (${selected.get('green_fee', '?')})",
                terminal="dry_run_stopped",
                slot=selected,
            )
            queue.put({"type": "status", "status": "succeeded"})
            _finalize_worker()
            return

        submit_started = time.perf_counter()
        booking_response, booking_latency = booking.book_slot(
            idempotency_key=f"job-{job_id}-{selected.get('tee_time_id', 'unknown')}",
            slot=selected,
            schedule_id=resolved_schedule_id,
            date=payload["play_date"],
            players=payload["players"],
            holes=holes,
            request_id=request_id,
        )
        _emit_event(
            queue,
            phase="checkout",
            stage="booking_submit",
            message="Submitted booking reservation",
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
        _finalize_worker()

    except AntiBotDetectedError as exc:
        _emit_event(
            queue,
            phase="auth",
            stage="antibot",
            message=f"Anti-bot protection detected: {exc}. Attempting Playwright fallback...",
        )
        _run_playwright_fallback(job_id, payload, queue, base_url, username, password)
        _finalize_worker()

    except ForeUpError as exc:
        _emit_event(queue, phase="confirm", stage="terminal", message=f"ForeUp request failed: {exc}", terminal="failed")
        queue.put({"type": "status", "status": "failed"})
        _finalize_worker()
    except Exception as exc:  # noqa: BLE001
        _emit_event(queue, phase="confirm", stage="terminal", message=f"Unexpected worker error: {exc}", terminal="failed")
        queue.put({"type": "status", "status": "failed"})
        _finalize_worker()


def _run_playwright_fallback(
    job_id: str,
    payload: dict[str, Any],
    queue: Queue,
    base_url: str,
    username: str,
    password: str,
) -> None:
    try:
        from src.booking.playwright_fallback import PlaywrightBookingClient, PlaywrightBookingError

        booking_url = payload.get("foreup_booking_url") or base_url
        pw_client = PlaywrightBookingClient()

        try:
            pw_started = time.perf_counter()
            pw_client.login(booking_url, username, password)
            _emit_event(
                queue,
                phase="auth",
                stage="playwright_login",
                message="Playwright login successful",
                latency_ms=round((time.perf_counter() - pw_started) * 1000, 2),
            )

            find_started = time.perf_counter()
            pw_slots = pw_client.find_times(payload["play_date"], payload["players"])
            _emit_event(
                queue,
                phase="discovery",
                stage="playwright_discovery",
                message=f"Playwright found {len(pw_slots)} slots",
                latency_ms=round((time.perf_counter() - find_started) * 1000, 2),
            )

            matching = None
            for ps in pw_slots:
                t = ps.time[:5]
                if payload["time_start"] <= t <= payload["time_end"]:
                    matching = ps
                    break

            if not matching:
                _emit_event(queue, phase="confirm", stage="terminal", message="Playwright: no matching slots", terminal="failed")
                queue.put({"type": "status", "status": "failed"})
                return

            dry_run = payload.get("dry_run", True)
            result = pw_client.book_slot(matching, dry_run=dry_run)
            terminal = "dry_run_stopped" if dry_run else ("success" if result.get("status") == "confirmed" else "unknown")
            _emit_event(
                queue,
                phase="confirm",
                stage="playwright_booking",
                message=f"Playwright booking result: {result.get('status', 'unknown')}",
                terminal=terminal,
                confirmation=result,
            )
            queue.put({"type": "status", "status": "succeeded" if terminal != "failed" else "failed"})
        finally:
            pw_client.close()

    except ImportError:
        _emit_event(
            queue,
            phase="confirm",
            stage="terminal",
            message="Playwright not installed. Run: pip install playwright && playwright install chromium",
            terminal="failed",
        )
        queue.put({"type": "status", "status": "failed"})
    except Exception as exc:  # noqa: BLE001
        _emit_event(queue, phase="confirm", stage="terminal", message=f"Playwright fallback failed: {exc}", terminal="failed")
        queue.put({"type": "status", "status": "failed"})


def _watch_worker(job_id: str, queue: Queue, process: Process) -> None:
    worker_done = False
    while not worker_done or process.is_alive():
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
            elif msg["type"] == "worker_done":
                worker_done = True

    process.join(timeout=1)


# --- Routes ---

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/connect")
def connect(req: ConnectRequest) -> dict[str, Any]:
    base_url = os.getenv("FOREUP_BASE_URL", "https://foreupsoftware.com")
    schedule_id = req.schedule_id
    course_id = req.course_id

    if req.booking_url:
        base_url, parsed_sched, parsed_course = _extract_foreup_target(
            req.booking_url, base_url, schedule_id, course_id
        )
        schedule_id = schedule_id or parsed_sched
        course_id = course_id or parsed_course

    username = req.username or os.getenv("FOREUP_USERNAME", "")
    password = req.password or os.getenv("FOREUP_PASSWORD", "")

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    try:
        client = ForeUpDiscoveryClient(base_url=base_url)
        client.bootstrap_session(username=username, password=password, schedule_id=schedule_id, request_id=f"connect-{uuid.uuid4().hex[:8]}")
    except AntiBotDetectedError:
        raise HTTPException(status_code=403, detail="Anti-bot protection detected. This course may require Playwright fallback.")
    except ForeUpError as exc:
        raise HTTPException(status_code=401, detail=f"ForeUp login failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Connection error: {exc}")

    return {
        "connected": True,
        "base_url": base_url,
        "schedule_id": schedule_id,
        "course_id": course_id,
    }


@app.post("/api/jobs")
def start_job(req: StartJobRequest) -> dict[str, str]:
    job_id = uuid.uuid4().hex[:12]
    state = JobState(job_id=job_id)
    with JOB_LOCK:
        JOBS[job_id] = state

    payload = req.model_dump()
    payload.pop("password", None)
    safe_payload = {k: v for k, v in payload.items() if k != "password"}

    full_payload = req.model_dump()
    queue: Queue = Queue()
    proc = Process(target=_worker_main, args=(job_id, full_payload, queue), daemon=True)
    proc.start()

    watcher = Thread(target=_watch_worker, args=(job_id, queue, proc), daemon=True)
    watcher.start()

    with JOB_LOCK:
        _event(JOBS[job_id], "job", "Job started", request=safe_payload)

    return {"job_id": job_id}


@app.get("/api/courses")
def list_courses(q: str = "") -> dict[str, Any]:
    query = q.strip().lower()
    matches = []
    for course_id, settings in APP_CONFIG.courses.items():
        if query and query not in course_id.lower():
            continue
        matches.append(
            {
                "course_id": course_id,
                "display_name": course_id.replace("_", " ").title(),
                "default_players": settings.player_count,
                "schedule_id": settings.schedule_id,
                "foreup_course_id": settings.foreup_course_id,
                "booking_url": settings.booking_url,
                "holes": settings.holes,
                "poll_interval_seconds": settings.poll_interval_seconds,
                "target_windows": [{"start": w.start, "end": w.end} for w in settings.target_windows],
            }
        )
    return {"source": "config/courses.yaml", "courses": matches}


@app.get("/api/jobs/{job_id}")
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


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
