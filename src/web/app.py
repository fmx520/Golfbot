from __future__ import annotations

import os
from pathlib import Path as _Path

_env_file = _Path(__file__).resolve().parents[2] / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

import json as _json
import logging as _logging
import sys as _sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# All courses we target are East Coast — anchor sniper timing to NY tz so the
# bot fires at the right wall-clock minute regardless of where the server is
# deployed (Render runs containers in UTC by default).
EAST_COAST_TZ = ZoneInfo("America/New_York")
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import Process, Queue
from pathlib import Path
from queue import Empty
from threading import Event, Lock, Thread
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.booking.foreup_client import ForeUpBookingClient, classify_reservation_response
from src.config import load_config
from src.courses import COURSE_CATALOG, cached_preflight, run_preflight
from src.discovery.foreup_client import AntiBotDetectedError, ForeUpDiscoveryClient, ForeUpError, StaleSlotError
from src.discovery import checkout_scan
from src.discovery.grid import analyze_teesheet_grid
from src.telemetry import grid_store
from src.telemetry.race_log import RaceRecorder

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


class _StructuredFormatter(_logging.Formatter):
    """Render a log record and its `extra` fields as one JSON line.

    The clients call logger.info("foreup_request", extra={...}) with the stage,
    latency, status and retry count for every request. A stock formatter drops
    all of that, so this keeps the fields that make the record worth having.
    """

    _BUILTIN = frozenset(_logging.LogRecord("", 0, "", 0, "", None, None).__dict__)

    def format(self, record: _logging.LogRecord) -> str:
        payload = {"event": record.getMessage(), "level": record.levelname, "logger": record.name}
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN and key not in ("message", "asctime"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[:2000]
        try:
            return "GOLFBOT_LOG " + _json.dumps(payload, default=str)
        except Exception:
            return f"GOLFBOT_LOG {{\"event\": {record.getMessage()!r}}}"


def _configure_logging() -> None:
    """Send app logs to stdout.

    Nothing configured a handler, so every _log() call in the ForeUp clients —
    per-request stage, latency, HTTP status, retry count — was being discarded.
    That instrumentation has never produced a line in production, which is why a
    slow request could not be broken down after the fact. Race records survived
    only because they write to stdout directly, bypassing logging entirely.

    Off by default in tests via LOG_LEVEL, and idempotent so repeated imports do
    not stack handlers and duplicate every line.
    """
    root = _logging.getLogger()
    if any(getattr(h, "_golfbot", False) for h in root.handlers):
        return
    handler = _logging.StreamHandler(_sys.stdout)
    handler.setFormatter(_StructuredFormatter())
    handler._golfbot = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    # Uvicorn already prints its own access lines; leave them alone.
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        _logging.getLogger(noisy).propagate = False


_configure_logging()

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
    booking_class: str = ""
    sniper_mode: bool = False
    release_time: str = ""
    auto_book: bool = False
    anticaptcha_api_key: str = ""
    course_ids: list[str] = Field(default_factory=list)
    ranking: str = "earliest"
    preselected_slot: dict[str, Any] | None = None
    # Future Sight: derive pre-stage targets from the user's time window and the
    # course's learned grid, and fire them as direct holds at the release
    # instant. Requires auto_book. prestage_times overrides with explicit times
    # (API-only — the UI never sends it; users cannot know a course's grid).
    future_sight: bool = False
    prestage_times: list[str] = Field(default_factory=list)


@dataclass
class JobState:
    job_id: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    events: list[dict[str, Any]] = field(default_factory=list)
    # Which race this job entered, and its slot in the queue of our own users
    # who entered the same one. Derived from JOBS rather than a second registry
    # so a finished job stops being a peer the moment its status changes.
    racing_key: tuple | None = None
    peer_ordinal: int = 0


# A job stops competing once it reaches one of these.
FINISHED_STATUSES = {"succeeded", "failed", "cancelled", "awaiting_payment"}


def _racing_key(payload: dict[str, Any]) -> tuple:
    """Identity of the race a job is entering.

    Strict equality on every field that changes what ForeUp returns. Two jobs
    coordinate only when they are asking the identical question — a resident
    and a non-resident booking class are looking at different inventory, so
    steering one off a slot could push it toward a time that does not exist in
    its class. Anything that does not match exactly simply does not coordinate,
    which is the current behavior.
    """
    courses = tuple(sorted(str(c) for c in (payload.get("course_ids") or []) if c))
    return (
        courses or (str(payload.get("schedule_id") or ""),),
        str(payload.get("play_date") or ""),
        str(payload.get("booking_class") or ""),
        int(payload.get("players") or 0),
        int(payload.get("holes") or 0),
    )


def _claim_peer_ordinal(job_id: str, key: tuple) -> tuple[int, int]:
    """Assign this job its place among our own jobs racing the same slots.

    Caller must hold JOB_LOCK. Returns (ordinal, peer_count). Ordinal is the
    lowest free position, not the peer count -- when an earlier job finishes,
    reusing its number keeps the set contiguous, where counting live peers
    would hand out a duplicate and put two of our users back on one slot.
    """
    peers = [
        j for j in JOBS.values()
        if j.job_id != job_id and j.racing_key == key and j.status not in FINISHED_STATUSES
    ]
    taken = {j.peer_ordinal for j in peers}
    ordinal = next(i for i in range(len(peers) + 1) if i not in taken)
    return ordinal, len(peers) + 1


def _hhmm_to_min(value: str) -> int | None:
    """'07:06' -> 426. Tolerates a leading date and returns None on junk."""
    text = (value or "").strip()
    if " " in text:
        text = text.split(" ", 1)[1]
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def _sample_covers(first: str, last: str, start: str, end: str) -> bool:
    """Did an observed sample actually span the hours we intend to fire into?

    A tee sheet is not guaranteed to run one interval all day. Bethpage Red
    runs 9 minutes at twilight and 10 in the morning, so a reference day whose
    only open times were 17:00-18:21 yields a perfectly self-consistent
    9-minute grid that is simply wrong about 7am. Extrapolating across that
    boundary is how we came to fire at 07:06 and 07:15 into a 10-minute sheet,
    where those times do not exist at all and ForeUp answers 400.
    """
    lo, hi = _hhmm_to_min(first), _hhmm_to_min(last)
    ws, we = _hhmm_to_min(start), _hhmm_to_min(end)
    if None in (lo, hi, ws, we):
        return False
    return lo <= we and ws <= hi


def _template_near(slots: list[dict[str, Any]], when: str) -> dict[str, Any]:
    """Payload template from the observed slot closest in time to `when`.

    green_fee and the side/class ids ride on the individual slot and are not
    constant across a day -- a twilight slot carries a twilight price. Taking
    the sheet's first slot meant a morning hold could go out describing itself
    as a 5pm twilight round, which is its own way to be rejected.
    """
    target = _hhmm_to_min(when)
    best, best_gap = {}, None
    for slot in slots:
        minutes = _hhmm_to_min(slot.get("time", ""))
        if minutes is None:
            continue
        gap = abs(minutes - target) if target is not None else 0
        if best_gap is None or gap < best_gap:
            best, best_gap = (slot.get("raw") or {}), gap
    return best


def _rotate(items: list[Any], ordinal: int) -> list[Any]:
    """Rotate a preference list so concurrent jobs open on different slots.

    Nothing is removed -- every job still tries the whole list and still falls
    through to what is left. Only the opening pick moves, which is the one that
    decides who dogpiles whom.
    """
    if ordinal <= 0 or len(items) < 2:
        return items
    shift = ordinal % len(items)
    return items[shift:] + items[:shift]


JOBS: dict[str, JobState] = {}
CMD_QUEUES: dict[str, Queue] = {}
# Kept so a running job can actually be stopped. A cancel is cooperative first
# — the worker checks for it at a safe point and exits cleanly, flushing its
# race record — with a hard terminate only as a fallback.
JOB_PROCS: dict[str, Process] = {}
JOB_LOCK = Lock()

# How long a worker gets to stop on its own before it is killed. One poll cycle
# is ~250ms, so this is many cycles of headroom; the only thing that legitimately
# takes longer is an in-flight booking, which we want to let finish.
CANCEL_GRACE_SECONDS = 10.0
APP_CONFIG = load_config()


def _event(job: JobState, stage: str, message: str, **extra: Any) -> None:
    job.updated_at = datetime.now(timezone.utc).isoformat()
    job.events.append({"ts": job.updated_at, "stage": stage, "message": message, **extra})


# Stages whose message is a diagnosis we will want AFTER the run, not just a
# line in the browser. Every one of these decides whether Future Sight fires,
# and until now the reason lived only in the page's event log -- so a lost race
# left no record of why we fell back to polling, and three days of races looked
# identical in the database. They now also go to stdout, which Render keeps.
# Set by the sniper worker at startup. Each race runs in its own process, so a
# single-slot module global is unambiguous -- there is never a second race here.
_race_for_decisions: list[Any] = []

_PERSISTED_EVENT_STAGES = frozenset({
    "prestage_disabled", "prestage_grid", "prestage_window", "prestage_capped",
    "prestage_armed", "prestage_rotated", "prestage_fire", "prestage_exhausted",
    "prestage_released", "multi_course",
})


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
    if stage in _PERSISTED_EVENT_STAGES:
        try:
            _logging.getLogger("src.web.sniper").info(
                "prestage_decision", extra={"stage": stage, "detail": message})
        except Exception:  # noqa: BLE001 -- telemetry must never break a race
            pass
        # Render's log store outlives a spin-down, but its retention is finite
        # and it is not keyed by run_id -- correlating a decision to a race
        # means timestamp-matching, which is how this got misdiagnosed twice.
        # The race row is the durable, queryable home for it.
        race = _race_for_decisions[0] if _race_for_decisions else None
        if race is not None:
            try:
                race.record_decision(stage, message)
            except Exception:  # noqa: BLE001 -- telemetry must never break a race
                pass
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
    def _slot_time_str(slot: dict[str, Any]) -> str:
        t = slot.get("time", "")
        if " " in t:
            t = t.split(" ", 1)[1]
        return t[:5]

    def _slot_price(slot: dict[str, Any]) -> float:
        raw = slot.get("raw") or {}
        for key in ("green_fee", "green_fee_18", "guest_green_fee"):
            val = raw.get(key, slot.get(key))
            if val not in (None, "", 0, "0"):
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
        return 1e9

    candidates = []
    for slot in slots:
        slot_time = _slot_time_str(slot)
        spots = int(slot.get("available_spots", slot.get("spots", 0)) or 0)
        if spots < payload["players"]:
            continue
        if not (payload["time_start"] <= slot_time <= payload["time_end"]):
            continue
        candidates.append(slot)

    if not candidates:
        return None

    ranking = payload.get("ranking", "earliest")
    if ranking == "cheapest":
        candidates.sort(key=lambda s: (_slot_price(s), _slot_time_str(s)))
    elif ranking == "priority":
        candidates.sort(key=lambda s: (s.get("_course_priority", 999), _slot_time_str(s)))
    else:  # earliest
        candidates.sort(key=lambda s: _slot_time_str(s))

    # Every ranking is deterministic, so our own concurrent users sort the
    # identical list and pick the identical slot -- six jobs get one chance
    # tried six times, with five guaranteed "Time not available". Rotating by
    # the arm-time ordinal spreads the opening pick across the window.
    return _rotate(candidates, int(payload.get("peer_ordinal") or 0))[0]


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


def _worker_main(job_id: str, payload: dict[str, Any], queue: Queue, cmd_queue: Queue | None = None) -> None:
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
            discovery.bootstrap_session(username=username, password=password, schedule_id=resolved_schedule_id, course_id=resolved_course_id, request_id=request_id)
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
        default_teesheet = resolved_course_id or resolved_schedule_id
        teesheet_id = default_teesheet  # also referenced in error/verify paths below
        preselected = payload.get("preselected_slot")

        if preselected:
            slots = [preselected]
            course_ids = [str(preselected.get("_source_teesheet_id") or default_teesheet)]
            _emit_event(
                queue,
                phase="discovery",
                stage="preselected_slot",
                message=f"Using preselected slot from prior dry run: {preselected.get('time')}",
                latency_ms=round((time.perf_counter() - fetch_started) * 1000, 2),
            )
        else:
            course_ids = [c for c in (payload.get("course_ids") or []) if c] or [default_teesheet]
            slots = []
            requested_class = payload.get("booking_class", "")
            for priority_index, tid in enumerate(course_ids):
                resolved_class = discovery.resolve_booking_class(tid, requested_class)
                course_slots = discovery.lookup_availability(
                    schedule_id=tid,
                    date=payload["play_date"],
                    players=payload["players"],
                    holes=holes,
                    booking_class=resolved_class,
                    request_id=request_id,
                )
                for s in course_slots:
                    s["_course_priority"] = priority_index
                    s["_source_teesheet_id"] = tid
                slots.extend(course_slots)

            _emit_event(
                queue,
                phase="discovery",
                stage="course_availability_fetch",
                message=f"Fetched {len(slots)} available tee times across {len(course_ids)} course(s)",
                latency_ms=round((time.perf_counter() - fetch_started) * 1000, 2),
                courses=len(course_ids),
            )

        filter_started = time.perf_counter()
        selected = preselected if preselected else _select_slot(slots, payload)
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

        raw_slot = selected.get("raw", {}) or {}
        constraint_fields = {
            k: raw_slot.get(k) for k in (
                "minimum_players", "maximum_players_per_booking", "allowed_group_sizes",
                "available_spots", "available_spots_9", "available_spots_18",
                "booking_class_id", "require_credit_card", "pay_online",
                "booking_fee_required", "booking_fee_price",
            )
        }
        _emit_event(queue, phase="checkout", stage="debug",
                    message=f"Slot constraints: {constraint_fields} | requested players={payload.get('players')} holes={holes}")

        captcha_token: str | None = None
        anticaptcha_key = payload.get("anticaptcha_api_key") or os.getenv("ANTICAPTCHA_API_KEY", "")
        captcha_solver = None
        captcha_site_key: str | None = None
        captcha_page_url: str | None = None
        if anticaptcha_key:
            try:
                captcha_site_key = discovery.extract_recaptcha_sitekey(schedule_id=resolved_schedule_id)
                if captcha_site_key:
                    from src.captcha.anticaptcha import AntiCaptchaSolver  # noqa: F401
                    captcha_solver = AntiCaptchaSolver(api_key=anticaptcha_key)
                    captcha_page_url = (
                        payload.get("foreup_booking_url")
                        or f"{base_url}/index.php/booking/{resolved_schedule_id}/{resolved_course_id}"
                    )
                    _emit_event(queue, phase="captcha", stage="solving",
                                message=f"Solving reCAPTCHA (site_key={captcha_site_key[:12]}…)")
                    captcha_token = captcha_solver.solve(captcha_page_url, captcha_site_key, timeout=120)
                    _emit_event(queue, phase="captcha", stage="solved",
                                message="reCAPTCHA solved — token attached to booking")
                else:
                    _emit_event(queue, phase="captcha", stage="skipped",
                                message="No reCAPTCHA site key found on booking page")
            except Exception as exc:  # noqa: BLE001
                _emit_event(queue, phase="captcha", stage="error",
                            message=f"CAPTCHA solve failed: {exc} — proceeding without token")

        def _refresh_captcha_token() -> str | None:
            """Solve a fresh captcha token for retries (reCAPTCHA tokens are single-use)."""
            if not captcha_solver or not captcha_site_key or not captcha_page_url:
                return None
            try:
                _emit_event(queue, phase="captcha", stage="resolving",
                            message="Re-solving reCAPTCHA for retry (previous token consumed)")
                token = captcha_solver.solve(captcha_page_url, captcha_site_key, timeout=120)
                _emit_event(queue, phase="captcha", stage="resolved",
                            message="reCAPTCHA refreshed — retrying booking")
                return token
            except Exception as exc:  # noqa: BLE001
                _emit_event(queue, phase="captcha", stage="error",
                            message=f"CAPTCHA refresh failed: {exc} — retrying without token")
                return None

        submit_started = time.perf_counter()

        def _booking_debug(event: str, data: dict[str, Any]) -> None:
            _emit_event(queue, phase="checkout", stage=f"debug_{event}",
                        message=f"{event}: {data}")

        customer_id = discovery.get_customer_id() if hasattr(discovery, "get_customer_id") else None

        def _wait_for_code(ttid: str) -> str:
            _emit_event(queue, phase="verify", stage="awaiting_code",
                        message=f"Booking code emailed. Enter the code to confirm reservation (5 min timeout). TTID={ttid}",
                        terminal="awaiting_code", reservation_id=ttid)
            queue.put({"type": "status", "status": "awaiting_input"})
            if cmd_queue is None:
                raise ForeUpError("No command queue available to receive booking code")

            # Background TTID refresh keeps the slot hold alive while we wait
            stop_refresh = Event()

            def _refresh_loop() -> None:
                while not stop_refresh.wait(8.0):
                    booking.refresh_pending_reservation(pending_reservation_id=ttid, request_id=request_id)

            refresher = Thread(target=_refresh_loop, daemon=True)
            refresher.start()
            try:
                cmd = cmd_queue.get(timeout=300)
            except Empty:
                stop_refresh.set()
                raise ForeUpError("Booking code timeout — no code received within 5 minutes")
            stop_refresh.set()

            code = cmd.get("code", "")
            _emit_event(queue, phase="verify", stage="submitting_code",
                        message=f"Submitting booking code: {code}")
            queue.put({"type": "status", "status": "running"})
            return code

        request_code_callback = _wait_for_code if customer_id else None

        # ---- Stale-slot retry loop: if the chosen slot gets taken between
        # discovery and booking, fall back to the next-best slot per ranking
        # rather than terminating. Re-discovers if the initial pool is
        # exhausted (skipped when a preselected slot was provided). ----
        def _slot_key(s: dict[str, Any]) -> tuple:
            raw = s.get("raw") or {}
            return (str(raw.get("teesheet_id") or s.get("_source_teesheet_id", "")), s.get("time", ""))

        tried_keys: set[tuple] = {_slot_key(selected)}
        current_pool = list(slots)
        max_attempts = 5
        booking_response = None
        booking_latency = None
        last_stale_msg = ""

        for attempt in range(1, max_attempts + 1):
            slot_raw = selected.get("raw") or {}
            slot_schedule_id = str(slot_raw.get("schedule_id") or slot_raw.get("teesheet_id") or selected.get("_source_teesheet_id") or default_teesheet)
            slot_course_id = str(slot_raw.get("course_id") or resolved_schedule_id)

            try:
                booking_response, booking_latency = booking.book_slot(
                    idempotency_key=f"job-{job_id}-{slot_schedule_id}-{selected.get('time', 'unknown')}",
                    slot=selected,
                    schedule_id=slot_schedule_id,
                    course_id=slot_course_id,
                    date=payload["play_date"],
                    players=payload["players"],
                    holes=holes,
                    request_id=request_id,
                    # Skip pre-validation when we already discovered this slot
                    # this run — the retry loop handles stale-slot exceptions
                    # downstream, so the extra GET only adds latency.
                    skip_validation=True,
                    captcha_token=captcha_token,
                    debug_callback=_booking_debug,
                    customer_id=customer_id,
                    request_code_callback=request_code_callback,
                )
                break
            except StaleSlotError as exc:
                last_stale_msg = str(exc)
                _emit_event(queue, phase="checkout", stage="slot_taken",
                            message=f"Slot {selected.get('time')} on teesheet {slot_schedule_id} was taken. Trying next-best (attempt {attempt}/{max_attempts}).",
                            stale_error=last_stale_msg)
                captcha_token = _refresh_captcha_token() or captcha_token
            except ForeUpError as exc:
                msg = str(exc).lower()
                stale_keywords = ("no longer available", "not available", "already booked", "0 spots")
                captcha_keywords = ("captcha", "recaptcha", "g-recaptcha")
                if any(k in msg for k in stale_keywords):
                    last_stale_msg = str(exc)
                    _emit_event(queue, phase="checkout", stage="slot_taken",
                                message=f"Slot {selected.get('time')} unavailable: {exc}. Trying next-best (attempt {attempt}/{max_attempts}).",
                                stale_error=last_stale_msg)
                    captcha_token = _refresh_captcha_token() or captcha_token
                elif any(k in msg for k in captcha_keywords):
                    last_stale_msg = str(exc)
                    new_token = _refresh_captcha_token()
                    if not new_token:
                        raise
                    captcha_token = new_token
                    # Retry the same slot with a fresh token; don't drop it.
                    tried_keys.discard(_slot_key(selected))
                    continue
                else:
                    raise

            # Pick next-best slot, re-discovering if needed (and allowed).
            candidates = [s for s in current_pool if _slot_key(s) not in tried_keys]
            if not candidates and not preselected:
                _emit_event(queue, phase="discovery", stage="rediscover",
                            message="Initial slot pool exhausted — re-fetching availability.")
                rediscovered: list[dict[str, Any]] = []
                for priority_index, tid in enumerate(course_ids):
                    resolved_class = discovery.resolve_booking_class(tid, payload.get("booking_class", "")) if hasattr(discovery, "resolve_booking_class") else payload.get("booking_class", "")
                    course_slots = discovery.lookup_availability(
                        schedule_id=tid,
                        date=payload["play_date"],
                        players=payload["players"],
                        holes=holes,
                        booking_class=resolved_class,
                        request_id=request_id,
                    )
                    for s in course_slots:
                        s["_course_priority"] = priority_index
                        s["_source_teesheet_id"] = tid
                    rediscovered.extend(course_slots)
                current_pool = rediscovered
                candidates = [s for s in current_pool if _slot_key(s) not in tried_keys]

            if not candidates:
                _emit_event(queue, phase="confirm", stage="terminal",
                            message=f"No more slots match your window after {attempt} attempt(s). Last error: {last_stale_msg}",
                            terminal="failed")
                queue.put({"type": "status", "status": "failed"})
                _finalize_worker()
                return

            selected = _select_slot(candidates, payload)
            if not selected:
                _emit_event(queue, phase="confirm", stage="terminal",
                            message=f"No slots match your window after {attempt} attempt(s).",
                            terminal="failed")
                queue.put({"type": "status", "status": "failed"})
                _finalize_worker()
                return
            tried_keys.add(_slot_key(selected))
        else:
            _emit_event(queue, phase="confirm", stage="terminal",
                        message=f"Reached max booking attempts ({max_attempts}) — all candidate slots were taken.",
                        terminal="failed")
            queue.put({"type": "status", "status": "failed"})
            _finalize_worker()
            return

        if booking_response is None:
            queue.put({"type": "status", "status": "failed"})
            _finalize_worker()
            return
        _emit_event(
            queue,
            phase="checkout",
            stage="booking_submit",
            message="Submitted booking reservation",
            latency_ms=round((time.perf_counter() - submit_started) * 1000, 2),
            provider_latency_ms=booking_latency.total_ms,
        )

        resp_str = str(booking_response).lower()
        needs_code = (
            booking_response.get("requires_code")
            or "booking_code" in resp_str
            or "verification" in resp_str
            or "booking code" in resp_str
        )

        if needs_code and cmd_queue is not None:
            reservation_id = booking_response.get("reservation_id", booking_response.get("id", ""))
            _emit_event(
                queue, phase="verify", stage="awaiting_code",
                message=f"Verification code sent to your email — enter it below (5 min timeout). Response: {booking_response}",
                terminal="awaiting_code",
            )
            queue.put({"type": "status", "status": "awaiting_input"})

            try:
                cmd = cmd_queue.get(timeout=300)
                code = cmd.get("code", "")
                _emit_event(queue, phase="verify", stage="submitting_code", message=f"Submitting verification code: {code}")
                queue.put({"type": "status", "status": "running"})

                verify_response = booking.submit_booking_code(
                    code=code,
                    reservation_id=str(reservation_id),
                    schedule_id=teesheet_id,
                    request_id=request_id,
                )
                _emit_event(
                    queue, phase="confirm", stage="booking_confirmation",
                    message=f"Verification submitted — response: {verify_response}",
                    terminal="success", confirmation=verify_response,
                )
                queue.put({"type": "status", "status": "succeeded"})
            except Empty:
                _emit_event(queue, phase="confirm", stage="terminal",
                            message="Verification code timeout — slot hold expired", terminal="failed")
                queue.put({"type": "status", "status": "failed"})
            _finalize_worker()
            return

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
        msg = str(exc)
        if "booking code" in msg.lower() or "requires a booking code" in msg.lower():
            _emit_event(
                queue,
                phase="verify",
                stage="booking_code_required",
                message=(
                    "This course requires an email booking code (2FA). The bot does not yet "
                    "implement the request-code endpoint for this course. The slot was held briefly "
                    "but will be released. To finish booking, complete it manually on foreupsoftware.com."
                ),
                terminal="failed",
                provider_message=msg,
            )
        else:
            _emit_event(queue, phase="confirm", stage="terminal",
                        message=f"ForeUp request failed: {exc} [schedule={resolved_schedule_id}, teesheet={teesheet_id}]",
                        terminal="failed")
        queue.put({"type": "status", "status": "failed"})
        _finalize_worker()
    except Exception as exc:  # noqa: BLE001
        _emit_event(queue, phase="confirm", stage="terminal", message=f"Unexpected worker error: {exc}", terminal="failed")
        queue.put({"type": "status", "status": "failed"})
        _finalize_worker()


def _sniper_worker_main(job_id: str, payload: dict[str, Any], queue: Queue, cmd_queue: Queue | None = None) -> None:
    """Sniper mode: pre-auth, wait for release, poll aggressively, grab slot."""
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
    teesheet_id = resolved_course_id or resolved_schedule_id
    # Sniper round-robins through every selected course at the same total
    # poll rate (5/sec), so coverage scales with however many the user checked
    # without burning extra request budget. Falls back to the URL's single
    # course when no checklist was provided.
    sniper_course_ids: list[str] = [c for c in (payload.get("course_ids") or []) if c] or [teesheet_id]
    auto_book = payload.get("auto_book", False)
    release_time_str = payload.get("release_time", "")
    play_date = payload["play_date"]
    anticaptcha_key = payload.get("anticaptcha_api_key") or os.getenv("ANTICAPTCHA_API_KEY", "")

    if not resolved_schedule_id:
        _emit_event(queue, phase="confirm", stage="terminal", message="No schedule_id provided.", terminal="failed")
        queue.put({"type": "status", "status": "failed"})
        _finalize_worker()
        return

    from datetime import date as date_cls

    now = datetime.now(EAST_COAST_TZ)
    if release_time_str:
        parts = release_time_str.split(":")
        release_hh = int(parts[0])
        release_mm = int(parts[1]) if len(parts) > 1 else 0
        release_dt = now.replace(hour=release_hh, minute=release_mm, second=0, microsecond=0)
        if release_dt <= now:
            release_dt = release_dt + timedelta(days=1)
        _emit_event(
            queue,
            phase="auth",
            stage="sniper_timer",
            message=f"Sniper armed for {release_dt.strftime('%I:%M:%S %p %Z')} (release_dt={release_dt.isoformat()})",
        )
    else:
        # "Snipe now" mode — start polling immediately, no release wait.
        release_dt = now
        _emit_event(
            queue,
            phase="auth",
            stage="sniper_timer",
            message="Snipe-now mode — polling starts immediately, no release wait",
        )

    request_id = f"sniper-{job_id}"
    discovery = ForeUpDiscoveryClient(base_url=base_url)
    captcha_solver = None

    race = RaceRecorder(
        run_id=uuid.uuid4().hex[:12],
        job_id=job_id,
        mode="sniper" if release_time_str else "snipe_now",
        course_ids=list(sniper_course_ids),
        play_date=play_date,
        release_at=release_dt.isoformat(),
        auto_book=bool(auto_book and not payload.get("dry_run", True)),
        peer_ordinal=int(payload.get("peer_ordinal") or 0),
        peer_count=int(payload.get("peer_count") or 1),
    )

    _race_for_decisions[:] = [race]

    # Best teesheet sample this race saw. A freshly released sheet is the most
    # complete view of a course's grid we ever get -- far better than a
    # reference day that is half booked -- so it is worth banking. Collected in
    # memory during the race (a length compare, nothing more) and written only
    # once the race is decided, never on the hot path.
    grid_sample: dict[str, Any] = {"times": [], "template": {}, "course": ""}

    # ForeUp hands every client the booking-fee flags on the slot itself. Until
    # recently its backend did not act on them, so a reservation posted with no
    # payment went through; it now refuses the conversion, which costs the whole
    # race and reads as a narrow loss. The flag is knowable before the release,
    # so say so then rather than at T+9s.
    fee_warned: list[bool] = [False]

    def _warn_if_fee_required(raw: dict[str, Any]) -> None:
        if fee_warned[0] or not isinstance(raw, dict):
            return
        if not raw.get("booking_fee_required"):
            return
        fee_warned[0] = True
        price = raw.get("booking_fee_price")
        per = " per player" if raw.get("booking_fee_per_person") else ""
        cost = f"${price}{per}" if price else "a booking fee"
        _emit_event(queue, phase="sniper", stage="booking_fee_required",
                    message=f"Heads up: this course charges {cost} at checkout. We can win the "
                            f"hold, but the reservation we send carries no payment, so ForeUp "
                            f"will refuse to convert it — the race is winnable and the booking "
                            f"is not. Nothing here is a speed problem.")

    def _learn_grid_from_race() -> None:
        """Bank what this race saw about the course's grid. Never raises."""
        try:
            if len(grid_sample["times"]) < 3:
                return
            observed = analyze_teesheet_grid(grid_sample["times"])
            if not (observed.interval_minutes and observed.consistent):
                return
            play_dow = datetime.strptime(play_date, "%Y-%m-%d").date().weekday()
            if grid_store.upsert_grid(course_id=str(grid_sample["course"]),
                                      day_of_week=play_dow, grid=observed.as_dict(),
                                      template=grid_sample["template"],
                                      source_date=play_date):
                _emit_event(queue, phase="sniper", stage="grid_learned",
                            message=f"Learned this course's {observed.interval_minutes}-minute grid "
                                    f"from the released sheet ({observed.observed_count} times seen). "
                                    f"Future Sight can derive targets here from now on, even if the "
                                    f"reference day is booked solid.")
        except Exception:  # noqa: BLE001 — the registry must never affect a race
            pass

    def _flush_race() -> None:
        """Emit the race record and surface the one-line verdict in the UI."""
        # Every terminal path funnels through here, so this is the one place
        # that guarantees a race's grid observation is banked exactly once. It
        # no-ops on the arming-failure paths, where nothing was polled yet.
        _learn_grid_from_race()
        try:
            record = race.flush()
            _emit_event(queue, phase="confirm", stage="race_summary",
                        message=record["summary"], race=record)
        except Exception:  # noqa: BLE001 — telemetry must never kill a run
            pass

    def _cancel_requested() -> bool:
        """Non-blocking check for a user stop request.

        Checked only at safe points — never between the pending_reservation and
        the reservation submit, where stopping would strand a hold on ForeUp's
        side or drop a booking that actually succeeded.
        """
        if cmd_queue is None:
            return False
        try:
            msg = cmd_queue.get_nowait()
        except Empty:
            return False
        if isinstance(msg, dict) and msg.get("type") == "cancel":
            return True
        # Something else (a booking code) — hand it back for the 2FA flow.
        cmd_queue.put(msg)
        return False

    cancelled = False

    try:
        auth_started = time.perf_counter()
        if username and password:
            discovery.bootstrap_session(
                username=username, password=password,
                schedule_id=resolved_schedule_id, course_id=resolved_course_id,
                request_id=request_id,
            )
            _emit_event(queue, phase="auth", stage="session",
                        message="ForeUp session bootstrapped",
                        latency_ms=round((time.perf_counter() - auth_started) * 1000, 2))
        else:
            _emit_event(queue, phase="auth", stage="session", message="No credentials — unauthenticated mode",
                        latency_ms=round((time.perf_counter() - auth_started) * 1000, 2))

        # ---- Future Sight arming: validate targets against the course's real
        # grid and cache the per-course payload fields, all BEFORE the release
        # so a bad target fails now, not at 7pm. ----
        def _norm_hhmm(t: str) -> str:
            parts = t.strip().split(":")
            try:
                return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
            except (ValueError, IndexError):
                return t.strip()

        prestage_plan: dict[str, Any] | None = None
        fs_enabled = bool(payload.get("future_sight"))
        raw_targets = [_norm_hhmm(t) for t in (payload.get("prestage_times") or []) if t and t.strip()]

        # Pre-staging only beats polling when the targets are known in advance.
        # At a release every time opens at once, so the whole window is a valid
        # target list. Started mid-day, exactly one unknown slot has opened —
        # firing at six times to find out which is a shotgun, while a single
        # poll would just say. Burning the burst first also delays the polling
        # that would have won.
        if (fs_enabled or raw_targets) and not release_time_str:
            _emit_event(queue, phase="sniper", stage="prestage_disabled",
                        message="Future Sight needs a release time — started now, there is no known "
                                "moment when times open, so pre-staged holds would fire at slots that "
                                "are simply already booked. Running as Sniper (polling) instead, which "
                                "is the right tool for a mid-day cancellation.")
            fs_enabled = False
            raw_targets = []

        if (fs_enabled or raw_targets) and not (auto_book and not payload.get("dry_run", True)):
            _emit_event(queue, phase="sniper", stage="prestage_disabled",
                        message="Future Sight ignored — pre-staged holds are real, so it "
                                "requires Auto-book ON and Dry Run OFF.")
            fs_enabled = False
            raw_targets = []
        if len(raw_targets) > 4:
            _emit_event(queue, phase="sniper", stage="prestage_capped",
                        message=f"Future Sight: {len(raw_targets)} targets given, using the first 4 "
                                f"({', '.join(raw_targets[:4])}). Holds fire one at a time and a "
                                f"rejection costs ~0.5s, so a long list just delays the poll fallback.")
            raw_targets = raw_targets[:4]

        def _gen_targets(interval: int, phase: int, cap: int = 6) -> list[str]:
            """Every real tee time inside the user's window, earliest first.

            The user sets a window; the course sets the grid. The intersection
            is the target list — nobody has to know that Red runs a 9-minute
            sheet anchored at :03.
            """
            def _mins(t: str) -> int:
                hh, mm = t.split(":")[:2]
                return int(hh) * 60 + int(mm)
            start, end = _mins(payload["time_start"]), _mins(payload["time_end"])
            first = start + ((phase - start) % interval)
            out = [f"{m // 60:02d}:{m % 60:02d}" for m in range(first, end + 1, interval)]
            if len(out) > cap:
                _emit_event(queue, phase="sniper", stage="prestage_window",
                            message=f"Future Sight: your window holds {len(out)} tee times — "
                                    f"pre-staging the first {cap} (earliest). Polling covers the rest.")
            return out[:cap]

        if raw_targets or fs_enabled:
            ps_course = sniper_course_ids[0]
            ps_class = payload.get("booking_class", "")
            try:
                if hasattr(discovery, "resolve_booking_class"):
                    ps_class = discovery.resolve_booking_class(ps_course, ps_class)
            except ForeUpError as exc:
                _emit_event(queue, phase="confirm", stage="terminal",
                            message=f"Future Sight: {exc}", terminal="failed")
                _flush_race()
                queue.put({"type": "status", "status": "failed"})
                _finalize_worker()
                return

            # The release date's teesheet is empty until it opens, so the grid
            # comes from a day that is already visible. Best reference: the same
            # day-of-week one week earlier — the booking window means it is
            # published right now, and weekend sheets can differ from weekday
            # ones. Fall back to tomorrow, then to the stored registry, so a
            # booked-solid reference day no longer means guessing.
            try:
                play_dt = datetime.strptime(play_date, "%Y-%m-%d").date()
            except ValueError:
                play_dt = (datetime.now(EAST_COAST_TZ) + timedelta(days=1)).date()
            today_east = datetime.now(EAST_COAST_TZ).date()
            week_before = play_dt - timedelta(days=7)
            ref_candidates = []
            if today_east <= week_before < play_dt:
                ref_candidates.append(week_before.isoformat())
            tomorrow = (today_east + timedelta(days=1)).isoformat()
            if tomorrow not in ref_candidates:
                ref_candidates.append(tomorrow)

            ref_slots: list[dict[str, Any]] = []
            ref_date = ""
            template: dict[str, Any] = {}
            for cand in ref_candidates:
                try:
                    found = discovery.lookup_availability(
                        schedule_id=ps_course, date=cand,
                        players=payload["players"], holes=holes, booking_class=ps_class,
                    )
                except ForeUpError as exc:
                    _emit_event(queue, phase="sniper", stage="prestage_grid",
                                message=f"Future Sight: teesheet read for {cand} failed ({exc}).")
                    continue
                if found:
                    ref_slots, ref_date = found, cand
                    break

            template_seed = (ref_slots[0].get("raw") or {}) if ref_slots else {}
            ref_grid = analyze_teesheet_grid(sl.get("time", "") for sl in ref_slots)
            play_dow = play_dt.weekday()
            win_start, win_end = payload["time_start"], payload["time_end"]

            # Bank a good live read whether or not we end up aiming with it.
            if ref_grid.interval_minutes and ref_grid.consistent:
                grid_store.upsert_grid(course_id=str(ps_course), day_of_week=play_dow,
                                       grid=ref_grid.as_dict(), template=template_seed,
                                       source_date=ref_date)

            stored = grid_store.fetch_grid(course_id=str(ps_course), day_of_week=play_dow)

            # ---- Pick which grid to aim with. ----
            # The live read used to win unconditionally, so an 8-slot twilight
            # sample beat a 40-observation grid learned from the real released
            # sheet. Choose on evidence instead: a sample that covers the hours
            # we are firing into beats one that does not, and more observations
            # break the tie.
            cands: list[dict[str, Any]] = []
            if ref_grid.interval_minutes and ref_grid.consistent:
                cands.append({
                    "interval": int(ref_grid.interval_minutes),
                    "phase": (_hhmm_to_min(ref_grid.first_time) or 0) % int(ref_grid.interval_minutes),
                    "first": ref_grid.first_time, "last": ref_grid.last_time,
                    "seen": ref_grid.observed_count, "template": template_seed,
                    "label": f"the {ref_grid.interval_minutes}-minute grid read live from {ref_date}",
                })
            if stored and stored.get("interval_minutes") and stored.get("anchor_phase") is not None:
                cands.append({
                    "interval": int(stored["interval_minutes"]),
                    "phase": int(stored["anchor_phase"]),
                    "first": stored.get("first_time", ""), "last": stored.get("last_time", ""),
                    "seen": int(stored.get("observed_count") or 0),
                    "template": stored.get("template") or {},
                    "label": f"the remembered {stored['interval_minutes']}-minute grid "
                             f"({grid_store.parse_hhmm_row(stored)})",
                })

            for c in cands:
                c["covers"] = _sample_covers(c["first"], c["last"], win_start, win_end)
            cands.sort(key=lambda c: (c["covers"], c["seen"]), reverse=True)
            chosen = cands[0] if cands else None

            # A grid seen across a wide span and many times is describing the
            # sheet, not one corner of it, so extrapolating past the observed
            # window is reasonable. A thin sample is not. Span is what matters:
            # 36 times over eight hours generalises; 8 over eighty minutes does
            # not, however self-consistent it looks.
            for c in cands:
                span = (_hhmm_to_min(c["last"]) or 0) - (_hhmm_to_min(c["first"]) or 0)
                c["trustworthy"] = c["covers"] or (c["seen"] >= 20 and span >= 240)
            cands.sort(key=lambda c: (c["covers"], c["trustworthy"], c["seen"]), reverse=True)
            chosen = cands[0] if cands else None

            if chosen and not chosen["trustworthy"]:
                # This used to disarm Future Sight outright, because the burst
                # ran to completion before polling started and a wrong grid
                # burned seconds the poll needed. On 2026-08-31 that is exactly
                # what happened: no Monday grid was banked for course 2431 yet,
                # this gate fired, we ran as Sniper, and our first hold went out
                # at T+801ms against a race decided inside the first 350ms.
                #
                # The burst is concurrent now. A guessed grid costs polling
                # nothing -- rejections come back in ~200ms on another thread
                # and hold nothing -- so the only rational move is to take the
                # free shot. We say it is a guess and fire anyway.
                _emit_event(queue, phase="sniper", stage="prestage_grid",
                            message=f"Future Sight: the tee times we can see run "
                                    f"{chosen['first']}–{chosen['last']} ({chosen['seen']} times), which "
                                    f"neither covers your {win_start}–{win_end} window nor spans enough "
                                    f"of the day to extrapolate from confidently — these targets are an "
                                    f"educated guess. Firing them anyway: the burst runs alongside "
                                    f"polling, so a wrong guess costs nothing and a right one wins. "
                                    f"Polling is unaffected either way.")

            if chosen:
                template = chosen["template"]
                if fs_enabled and not raw_targets:
                    raw_targets = _gen_targets(chosen["interval"], chosen["phase"])
                    if not raw_targets:
                        _emit_event(queue, phase="sniper", stage="prestage_window",
                                    message=f"Future Sight: no tee times on {chosen['label']} fall inside "
                                            f"your {win_start}–{win_end} window — widen it. Falling back "
                                            f"to pure polling.")
                off = [t for t in raw_targets
                       if (_hhmm_to_min(t) or 0) % chosen["interval"] != chosen["phase"] % chosen["interval"]]
                if off:
                    _emit_event(queue, phase="confirm", stage="terminal",
                                message=f"Future Sight: {', '.join(off)} are not on {chosen['label']}. "
                                        f"These times do not exist on the sheet — fix the targets and re-arm.",
                                terminal="failed")
                    _flush_race()
                    queue.put({"type": "status", "status": "failed"})
                    _finalize_worker()
                    return
                _warn_if_fee_required(chosen["template"])
                if raw_targets:
                    # Price and side ids vary across the day; take them from the
                    # observed slot nearest what we are actually going to fire at.
                    if ref_slots:
                        near = _template_near(ref_slots, raw_targets[0])
                        if near:
                            template = near
                    _emit_event(queue, phase="sniper", stage="prestage_armed",
                                message=f"Future Sight armed: {', '.join(raw_targets)} on course {ps_course} — "
                                        f"validated against {chosen['label']}, which covers "
                                        f"{chosen['first']}–{chosen['last']} ({chosen['seen']} times seen). "
                                        f"Holds fire at the release instant, before polling.")
            elif fs_enabled and not raw_targets:
                _emit_event(queue, phase="sniper", stage="prestage_grid",
                            message=f"Future Sight: no live reference sheet and nothing stored for "
                                    f"this course/day yet, so targets cannot be derived. Falling "
                                    f"back to pure polling. To teach it this course, press "
                                    f"\u201cShow this course\u2019s tee grid\u201d for a date whose sheet is "
                                    f"already published and has plenty of open times — the booking "
                                    f"API only returns OPEN slots, so a nearly full day hides the "
                                    f"real interval. Any release this bot polls also gets banked "
                                    f"automatically.")

            if raw_targets:
                ordinal = int(payload.get("peer_ordinal") or 0)
                rotated = _rotate(raw_targets, ordinal)
                if rotated != raw_targets:
                    _emit_event(queue, phase="sniper", stage="prestage_rotated",
                                message=f"Rotated to {', '.join(rotated)} — {payload.get('peer_count', 1)} "
                                        f"of our own jobs are pre-staging these exact criteria, and "
                                        f"targets are generated earliest-first, so unrotated we would all "
                                        f"fire at the same time in the same order. Same targets, "
                                        f"different opening shot.")
                raw_targets = rotated
                prestage_plan = {"times": raw_targets, "course": ps_course,
                                 "booking_class": ps_class, "template": template}

        captcha_token: str | None = None
        captcha_solved_at: float = 0
        captcha_solver = None
        captcha_site_key: str | None = None
        captcha_page_url: str = payload.get("foreup_booking_url") or f"{base_url}/index.php/booking/{resolved_schedule_id}/{resolved_course_id}"
        CAPTCHA_TOKEN_TTL = 110.0

        if auto_book and anticaptcha_key:
            site_key = discovery.extract_recaptcha_sitekey(schedule_id=resolved_schedule_id)
            captcha_site_key = site_key
            if site_key:
                from src.captcha.anticaptcha import AntiCaptchaSolver, CaptchaSolveError
                captcha_solver = AntiCaptchaSolver(api_key=anticaptcha_key)
                booking_page_url = captcha_page_url
                _emit_event(queue, phase="captcha", stage="solving",
                            message=f"reCAPTCHA detected (site key: {site_key[:12]}...) — solving via Anti-Captcha")
                try:
                    captcha_started = time.perf_counter()
                    captcha_token = captcha_solver.solve(booking_page_url, site_key, timeout=120)
                    captcha_solved_at = time.monotonic()
                    race.captcha_solves += 1
                    _emit_event(queue, phase="captcha", stage="solved",
                                message="CAPTCHA solved — token cached",
                                latency_ms=round((time.perf_counter() - captcha_started) * 1000, 2))
                except CaptchaSolveError as exc:
                    _emit_event(queue, phase="captcha", stage="failed",
                                message=f"CAPTCHA solve failed: {exc} — will attempt booking without token")
            else:
                _emit_event(queue, phase="captcha", stage="skipped",
                            message="No reCAPTCHA detected on booking page — skipping solver")

        seconds_until = (release_dt - datetime.now(EAST_COAST_TZ)).total_seconds()
        release_display = release_dt.strftime("%I:%M:%S %p %Z")
        _emit_event(queue, phase="sniper", stage="armed",
                    message=f"Armed — release at {release_display} ({int(seconds_until)}s from now)")

        # The re-warm below is a login round trip against ForeUp at its busiest
        # moment. A 5s lead was not enough headroom for it: when it overran, the
        # sleep-until-release below was skipped and we began polling *after* the
        # release, silently. Give it real room — waiting longer costs nothing
        # because we sleep off the remainder anyway.
        prewarm_lead = float(os.getenv("SNIPER_PREWARM_LEAD_SECONDS", "12"))
        wait_seconds = max(0, seconds_until - prewarm_lead)

        # If there is no waiting to do, the session was bootstrapped moments ago
        # and re-logging in buys nothing — but it costs 2-5s against ForeUp,
        # which last night pushed one race 5.2s past the release. On time with a
        # seconds-old session beats freshly logged in and late.
        skip_rewarm = wait_seconds <= 0
        if skip_rewarm and release_time_str and seconds_until > 0:
            _emit_event(queue, phase="sniper", stage="armed_late_warning",
                        message=(
                            f"Armed only {int(seconds_until)}s before the release — inside the "
                            f"{prewarm_lead:.0f}s pre-warm window. Skipping the session re-warm "
                            f"(login is still fresh from job start) so polling begins on time. "
                            f"Arm at least {prewarm_lead:.0f}s early next time."
                        ))

        if wait_seconds > 0:
            sleep_chunk = 2.0
            slept = 0.0
            last_update = 0.0
            while slept < wait_seconds:
                if _cancel_requested():
                    cancelled = True
                    break
                chunk = min(sleep_chunk, wait_seconds - slept)
                time.sleep(chunk)
                slept += chunk
                remaining = max(0, (release_dt - datetime.now(EAST_COAST_TZ)).total_seconds())
                if remaining <= 10 or (slept - last_update) >= 10:
                    _emit_event(queue, phase="sniper", stage="countdown",
                                message=f"Countdown: {int(remaining)}s remaining")
                    last_update = slept

                if captcha_token and captcha_solved_at and (time.monotonic() - captcha_solved_at) > CAPTCHA_TOKEN_TTL:
                    if captcha_solver:
                        _emit_event(queue, phase="captcha", stage="refreshing", message="CAPTCHA token expired — requesting fresh solve")
                        try:
                            site_key = captcha_site_key or discovery.extract_recaptcha_sitekey(schedule_id=resolved_schedule_id)
                            captcha_token = captcha_solver.solve(captcha_page_url, site_key, timeout=120)
                            captcha_solved_at = time.monotonic()
                            race.captcha_refreshes += 1
                            _emit_event(queue, phase="captcha", stage="solved", message="Fresh CAPTCHA token cached")
                        except Exception:
                            _emit_event(queue, phase="captcha", stage="failed", message="CAPTCHA refresh failed — will use expired token")

        # Anchor the release on the monotonic clock before the re-warm so every
        # offset below is measured against a fixed point that cannot be moved by
        # a wall-clock adjustment mid-race.
        release_monotonic = time.perf_counter() + (
            release_dt - datetime.now(EAST_COAST_TZ)
        ).total_seconds()
        race.mark_release_instant(release_monotonic)

        rewarm_started = time.perf_counter()
        if username and password and not skip_rewarm:
            discovery.refresh_session(
                schedule_id=resolved_schedule_id, course_id=resolved_course_id,
                username=username, password=password, request_id=request_id,
            )
            rewarm_ms = round((time.perf_counter() - rewarm_started) * 1000, 2)
            overran = time.perf_counter() > release_monotonic
            race.record_rewarm(rewarm_ms, overran)
            if overran:
                # This is the difference between racing and arriving after the
                # race. Say so loudly rather than letting it look like a normal
                # loss 90 seconds later.
                _emit_event(
                    queue, phase="auth", stage="rewarm_overran",
                    message=(
                        f"Session re-warm took {rewarm_ms:.0f}ms and overran the release "
                        f"by {(time.perf_counter() - release_monotonic) * 1000:.0f}ms — "
                        f"polling is starting LATE. Raise SNIPER_PREWARM_LEAD_SECONDS "
                        f"(currently {prewarm_lead:.0f}s)."
                    ),
                    latency_ms=rewarm_ms,
                )
            else:
                _emit_event(queue, phase="auth", stage="rewarm",
                            message="Session re-warmed",
                            latency_ms=rewarm_ms)

        if cancelled:
            race.aborted_reason = "cancelled_while_armed"
            _emit_event(queue, phase="confirm", stage="terminal",
                        message="Stopped before the release — no requests were sent to ForeUp.",
                        terminal="cancelled")
            _flush_race()
            queue.put({"type": "status", "status": "cancelled"})
            _finalize_worker()
            return

        remaining = release_monotonic - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)

        import random as _rand
        _emit_event(queue, phase="sniper", stage="polling", message="Polling started — jittered ~200ms interval")

        poll_base = 0.15
        poll_jitter = 0.15
        # How long polling keeps trying after the release. Env-tunable so a
        # race can be shortened without a deploy (and so tests can run).
        timeout = float(os.getenv("SNIPER_RACE_TIMEOUT_SECONDS", "90"))
        poll_start = time.perf_counter()
        poll_count = 0
        last_status_emit = 0.0
        selected = None  # last slot we attempted (for alert path)
        booked = False
        booking_response: dict[str, Any] | None = None

        do_auto_book = bool(auto_book and not payload.get("dry_run", True))
        booking = ForeUpBookingClient(base_url=base_url) if do_auto_book else None
        customer_id = discovery.get_customer_id() if hasattr(discovery, "get_customer_id") else None

        # Sniper retry state — tried slots survive across polls so we don't
        # thrash the same taken slot, and captcha re-solves are capped because
        # each one costs real time + an Anti-Captcha credit.
        tried_keys: set[tuple] = set()
        tried_labels: list[str] = []
        captcha_resolves = 0
        CAPTCHA_RESOLVE_CAP = 2
        retry_same_slot = False  # set when captcha refresh — retry same slot once

        def _slot_key(s: dict[str, Any]) -> tuple:
            raw = s.get("raw") or {}
            return (str(raw.get("teesheet_id") or s.get("_source_teesheet_id") or teesheet_id),
                    s.get("time", ""))

        def _sniper_wait_for_code(ttid: str) -> str:
            _emit_event(queue, phase="verify", stage="awaiting_code",
                        message=f"Booking code emailed. Enter the code to confirm reservation (5 min timeout). TTID={ttid}",
                        terminal="awaiting_code", reservation_id=ttid)
            queue.put({"type": "status", "status": "awaiting_input"})
            if cmd_queue is None:
                raise ForeUpError("No command queue available to receive booking code")
            stop_refresh = Event()
            race.two_factor_triggered = True
            code_wait_started = time.perf_counter()

            def _refresh_loop() -> None:
                # Background thread for the duration of the code wait — never on
                # the booking path, so recording outcomes here costs no latency.
                while not stop_refresh.wait(8.0):
                    if booking is None:
                        continue
                    err = booking.refresh_pending_reservation(
                        pending_reservation_id=ttid, request_id=request_id
                    )
                    if err:
                        race.hold_refresh_failed += 1
                        if not race.hold_refresh_first_error:
                            race.hold_refresh_first_error = err
                            _emit_event(
                                queue, phase="verify", stage="hold_refresh_failed",
                                message=(
                                    f"Hold keepalive is FAILING while waiting for the code "
                                    f"({err}). The reservation may already be gone."
                                ),
                            )
                    else:
                        race.hold_refresh_ok += 1

            refresher = Thread(target=_refresh_loop, daemon=True)
            refresher.start()
            try:
                cmd = cmd_queue.get(timeout=300)
            except Empty:
                stop_refresh.set()
                race.code_wait_ms = round((time.perf_counter() - code_wait_started) * 1000, 2)
                raise ForeUpError("Booking code timeout — no code received within 5 minutes")
            stop_refresh.set()
            race.code_wait_ms = round((time.perf_counter() - code_wait_started) * 1000, 2)
            if isinstance(cmd, dict) and cmd.get("type") == "cancel":
                raise ForeUpError("Stopped by user while awaiting the booking code")
            code = cmd.get("code", "")
            _emit_event(queue, phase="verify", stage="submitting_code",
                        message=f"Submitting booking code: {code}")
            queue.put({"type": "status", "status": "running"})
            return code

        request_code_callback = _sniper_wait_for_code if (customer_id and cmd_queue is not None) else None

        def _sniper_debug(event: str, data: dict[str, Any]) -> None:
            _emit_event(queue, phase="checkout", stage=f"debug_{event}",
                        message=f"{event}: {data}")

        # Pre-resolve booking class per teesheet — each course at a multi-
        # course facility (Bethpage) has its own booking_class IDs even
        # though the class names are identical. Resolving once up front
        # avoids re-doing the lookup on every poll.
        requested_class = payload.get("booking_class", "")
        per_course_class: dict[str, str] = {}
        for tid in sniper_course_ids:
            if hasattr(discovery, "resolve_booking_class"):
                per_course_class[tid] = discovery.resolve_booking_class(tid, requested_class)
            else:
                per_course_class[tid] = requested_class

        if len(sniper_course_ids) > 1:
            _emit_event(queue, phase="sniper", stage="multi_course",
                        message=f"Round-robin polling {len(sniper_course_ids)} courses: {', '.join(sniper_course_ids)}")

        # Keep a fresh captcha token in hand *during* the race. A solve takes
        # seconds, so it runs on a background thread: stalling the poll loop to
        # re-solve costs far more than the stale token does. Previously the only
        # refresh lived in the countdown loop, which does not run at all in
        # snipe-now mode and stops at T-prewarm otherwise — so a 90s race could
        # reach submit with an expired token and eat a re-solve at the worst
        # possible moment.
        captcha_state: dict[str, Any] = {"token": captcha_token, "solved_at": captcha_solved_at}
        stop_captcha_refresh = Event()

        def _captcha_refresh_loop() -> None:
            while not stop_captcha_refresh.wait(3.0):
                solved_at = captcha_state.get("solved_at") or 0
                if not captcha_solver or not solved_at:
                    continue
                # Refresh with headroom so the token is never near expiry at submit.
                if (time.monotonic() - solved_at) < (CAPTCHA_TOKEN_TTL - 25):
                    continue
                try:
                    site_key = captcha_site_key or discovery.extract_recaptcha_sitekey(schedule_id=resolved_schedule_id)
                    if not site_key:
                        continue
                    fresh = captcha_solver.solve(captcha_page_url, site_key, timeout=120)
                    captcha_state["token"] = fresh
                    captcha_state["solved_at"] = time.monotonic()
                    race.captcha_refreshes += 1
                except Exception:  # noqa: BLE001 — best effort; reactive path still covers us
                    pass

        captcha_refresher: Thread | None = None
        if captcha_solver and captcha_state.get("token"):
            captcha_refresher = Thread(target=_captcha_refresh_loop, daemon=True)
            captcha_refresher.start()

        # ---- Future Sight: pre-staged holds fire at T+0, CONCURRENTLY with polling.
        #
        # Two measured facts shape this block.
        #
        # 1. The burst used to run to completion BEFORE polling started, so a
        #    5s burst against a bad grid delayed the poll that would have won.
        #    That forced a gate refusing to fire unless the grid was already
        #    trusted -- and on 2026-08-31 the gate disarmed Future Sight (no
        #    Monday grid banked for course 2431 yet), we ran as Sniper, and our
        #    first hold went out at T+801ms into a race decided by T+350ms.
        #    On its own thread the burst costs polling nothing.
        #
        # 2. Targets used to fire one at a time. Measured 2026-08-30:
        #    T+0.000, T+0.568, T+2.443, T+4.204 -- only the first target was
        #    ever really in the race; the last was four seconds late. They now
        #    fire simultaneously, so every target is a shot at T+0.
        #
        # The thread only ACQUIRES holds. Completing one (2FA, payment) stays
        # on the main thread, so there is exactly one booking path and no way
        # to double-book or race two code prompts against each other.
        prestage_win: dict[str, Any] = {}
        prestage_win_lock = Lock()
        prestage_stop = Event()
        prestage_thread: Thread | None = None
        prestage_started_at = 0.0
        burst_seconds = float(os.getenv("SNIPER_PRESTAGE_BURST_SECONDS", "5"))

        def _release_hold(hold: dict[str, Any]) -> None:
            rid = str(hold.get("reservation_id") or "")
            if rid and booking is not None:
                booking.release_pending_reservation(
                    pending_reservation_id=rid, request_id=f"{request_id}-rel")

        def _claim_or_release(ps_time: str, hold: dict[str, Any]) -> None:
            """First accepted hold wins the race; every other one goes back.

            Firing all targets at once means we can win several. Squatting on
            the extras would take those times off the sheet for everyone --
            including the other people using this bot -- so they are handed
            straight back rather than left to expire.
            """
            with prestage_win_lock:
                if not prestage_win and not prestage_stop.is_set():
                    prestage_win.update({"time": ps_time, "hold": hold})
                    prestage_stop.set()
                    return
            _release_hold(hold)

        def _prestage_burst() -> None:
            times = list(prestage_plan["times"])
            tmpl = prestage_plan["template"]
            burst_pass = 0

            def _fire(ps_time: str) -> tuple[str, dict[str, Any]]:
                return ps_time, booking.probe_cold_hold(
                    target_time=f"{play_date} {ps_time}",
                    schedule_id=str(prestage_plan["course"]),
                    course_id=str(resolved_schedule_id),
                    players=payload["players"], holes=holes,
                    # The resolved class wins over the template's. The template
                    # comes from a reference DAY, and a course can run different
                    # booking classes on different days -- 2432 has been seen as
                    # both 2139 and 50295 -- so taking the template's value fires
                    # the hold under a class that may not apply to the play date,
                    # which ForeUp rejects outright.
                    booking_class_id=str(prestage_plan["booking_class"]
                                         or tmpl.get("booking_class_id", "")),
                    teesheet_side_id=str(tmpl.get("teesheet_side_id", "")),
                    green_fee=tmpl.get("green_fee", 0),
                    request_id=f"{request_id}-ps{burst_pass}",
                    stage="prestage_hold",
                )

            # Deliberately NOT calling _cancel_requested() here: it pops from
            # cmd_queue and pushes non-cancel messages back, so calling it off
            # the main thread can steal the 2FA code mid-booking. The burst
            # window is seconds, and the main loop sets prestage_stop the
            # moment it sees a cancel.
            while not prestage_stop.is_set():
                burst_pass += 1
                # A ForeUp release is not atomic at T+0.000 -- the sheet can
                # publish a beat late -- so the whole target list re-fires in
                # passes until the burst window closes. Rejections cost nothing
                # and hold nothing.
                race.record_first_poll(time.perf_counter())
                with ThreadPoolExecutor(max_workers=max(1, len(times))) as pool:
                    futures = [pool.submit(_fire, t) for t in times]
                    for fut in as_completed(futures):
                        try:
                            ps_time, hold = fut.result()
                        except Exception as exc:  # noqa: BLE001 -- one target failing is not fatal
                            race.record_prestage("?", "rejected", None, error=str(exc)[:600])
                            continue
                        hold_ms = hold.get("latency_ms") or 0.0
                        if hold.get("accepted"):
                            race.record_prestage(ps_time, "hold_accepted", hold_ms)
                            _claim_or_release(ps_time, hold)
                            continue
                        err = str(hold.get("error") or hold.get("response") or "")[:600]
                        race.record_prestage(ps_time, "rejected", hold_ms, error=err)
                        if burst_pass == 1:
                            _emit_event(queue, phase="sniper", stage="prestage_miss",
                                        message=f"Pre-staged {ps_time}: rejected in "
                                                f"{hold_ms:.0f}ms ({err})")

                if prestage_stop.is_set():
                    break
                if time.perf_counter() - prestage_started_at >= burst_seconds:
                    _emit_event(queue, phase="sniper", stage="prestage_exhausted",
                                message=f"All pre-staged targets missed across {burst_pass} pass(es) "
                                        f"({len(race.prestage)} fires in "
                                        f"{time.perf_counter() - prestage_started_at:.1f}s). "
                                        f"Polling has been running alongside the whole time.")
                    break
                prestage_stop.wait(0.25)

        def _complete_prestage_win(ps_time: str, hold: dict[str, Any]) -> bool:
            """Turn a won hold into a booking. Main thread only."""
            nonlocal booked, booking_response, selected
            tmpl = prestage_plan["template"]
            race.record_slot_seen(time.perf_counter())
            _emit_event(queue, phase="sniper", stage="prestage_hit",
                        message=f"PRE-STAGED HOLD ACCEPTED at {ps_time} in "
                                f"{hold.get('latency_ms') or 0:.0f}ms — completing booking.")
            submit_started = time.perf_counter()
            try:
                race.detect_to_submit_ms = 0.0
                prestage_response = booking.complete_prestaged_hold(
                    idempotency_key=f"prestage-{job_id}-{ps_time}",
                    pending_reservation_id=str(hold.get("reservation_id")),
                    target_time=f"{play_date} {ps_time}",
                    template=tmpl,
                    schedule_id=str(prestage_plan["course"]),
                    course_id=str(resolved_schedule_id),
                    players=payload["players"], holes=holes,
                    captcha_token=captcha_state.get("token"),
                    customer_id=customer_id,
                    request_code_callback=request_code_callback,
                    request_id=request_id,
                    debug_callback=_sniper_debug,
                )
                submit_ms = round((time.perf_counter() - submit_started) * 1000, 2)
                confirmed, reason = classify_reservation_response(prestage_response)
                race.booking_confirmed = confirmed
                race.submit_to_confirm_ms = submit_ms
                race.record_attempt(
                    ps_time,
                    "confirmed" if confirmed else ("failed" if confirmed is False else "unconfirmed"),
                    error="" if confirmed else reason,
                    latency_ms=submit_ms,
                    response=prestage_response,
                )
                race.booked = True
                booked = True
                booking_response = prestage_response
                selected = {"time": ps_time, "available_spots": payload["players"],
                            "green_fee": tmpl.get("green_fee", "?"), "raw": tmpl}
                if confirmed:
                    _emit_event(queue, phase="checkout", stage="booking_submit",
                                message=f"Booking confirmed via pre-staged hold at {ps_time}",
                                latency_ms=submit_ms)
                else:
                    _emit_event(queue, phase="checkout", stage="booking_unconfirmed",
                                message=f"Pre-staged submit at {ps_time} returned, but ForeUp's reply "
                                        f"does not confirm a reservation ({reason}). VERIFY IN YOUR "
                                        f"COURSE ACCOUNT before relying on this.",
                                latency_ms=submit_ms)
                return True
            except ForeUpError as exc:
                race.record_attempt(ps_time, "failed", error=str(exc),
                                    latency_ms=round((time.perf_counter() - submit_started) * 1000, 2))
                _emit_event(queue, phase="checkout", stage="prestage_submit_failed",
                            message=f"Pre-staged hold at {ps_time} could not be completed ({exc}) — "
                                    f"the hold will expire on its own. Polling continues.")
                # The burst has already wound down by the time we get here,
                # but polling never stopped -- it has been running throughout.
                return False

        def _take_prestage_win() -> tuple[str, dict[str, Any]] | None:
            if not prestage_win:
                return None
            with prestage_win_lock:
                if not prestage_win:
                    return None
                won = (str(prestage_win.get("time")), dict(prestage_win.get("hold") or {}))
                prestage_win.clear()
            return won

        if prestage_plan and booking is not None:
            prestage_started_at = time.perf_counter()
            _emit_event(queue, phase="sniper", stage="prestage_fire",
                        message=f"Future Sight: firing holds at {', '.join(prestage_plan['times'])} "
                                f"simultaneously at the release instant — no discovery round trip. "
                                f"Polling starts in parallel, so this costs the fallback nothing. "
                                f"Re-firing for up to {burst_seconds:.0f}s in case the sheet "
                                f"publishes late.")
            prestage_thread = Thread(target=_prestage_burst, daemon=True)
            prestage_thread.start()

        while not booked and not cancelled and time.perf_counter() - poll_start < timeout:
            # Top of the loop is the safe point: no hold is open and no booking
            # is in flight, so stopping here cannot strand anything.
            if _cancel_requested():
                cancelled = True
                break

            # A pre-staged hold landing beats anything polling can find, and it
            # is completed here rather than on the burst thread so there is only
            # ever one booking in flight. If completion fails the hold is gone
            # but polling never stopped, so we simply carry on.
            won = _take_prestage_win()
            if won and _complete_prestage_win(*won):
                break

            loop_start = time.perf_counter()
            poll_count += 1

            # Pick which course to poll this iteration. Rotation keeps the
            # total request rate identical to the single-course case.
            current_teesheet = sniper_course_ids[(poll_count - 1) % len(sniper_course_ids)]
            current_class = per_course_class.get(current_teesheet, requested_class)

            race.record_first_poll(loop_start)

            poll_result = "ok"
            try:
                slots = discovery.lookup_availability_fast(
                    schedule_id=current_teesheet,
                    date=play_date,
                    players=payload["players"],
                    holes=holes,
                    booking_class=current_class,
                    request_id=f"{request_id}-p{poll_count}",
                )
                # Tag each slot with the teesheet it came from so the
                # downstream book_slot uses the right course IDs.
                for s in slots:
                    s["_source_teesheet_id"] = current_teesheet
                if slots:
                    _warn_if_fee_required(slots[0].get("raw") or {})
                if len(slots) > len(grid_sample["times"]):
                    grid_sample["times"] = [s.get("time", "") for s in slots]
                    grid_sample["template"] = slots[0].get("raw") or {}
                    grid_sample["course"] = current_teesheet
            except AntiBotDetectedError as exc:
                # AntiBotDetectedError subclasses ForeUpError, so this used to be
                # swallowed by the generic handler below and recorded as "0 slots"
                # — a block and an empty teesheet looked identical in the logs and
                # in the UI. They are completely different problems.
                slots = []
                poll_result = "antibot"
                if race.polls.antibot == 0:
                    _emit_event(queue, phase="sniper", stage="antibot",
                                message=f"Anti-bot response on poll #{poll_count}: {exc}. "
                                        f"Polls are being rejected, not coming back empty — "
                                        f"continuing to poll but treat results as unreliable.")
            except ForeUpError:
                slots = []
                poll_result = "error"

            loop_ms = round((time.perf_counter() - loop_start) * 1000, 1)
            race.record_poll(loop_ms, len(slots), poll_result)

            untried = [s for s in slots if _slot_key(s) not in tried_keys] if not retry_same_slot else slots
            candidate = _select_slot(untried, payload) if untried else None

            if candidate:
                selected = candidate
                detect_time = time.perf_counter()
                race.record_slot_seen(detect_time)
                _emit_event(queue, phase="sniper", stage="found",
                            message=f"Poll #{poll_count}: {len(slots)} slots — TARGET FOUND: {candidate['time']} ({candidate['available_spots']} spots, ${candidate.get('green_fee', '?')})"
                                    + (f" | tried {len(tried_keys)}: {', '.join(tried_labels)}" if tried_labels else ""),
                            latency_ms=loop_ms, slot=candidate, tried_count=len(tried_keys))

                # No auto-book → first match is final; break and emit alert.
                if not do_auto_book:
                    break

                # Auto-book attempt — use the slot's *own* schedule/course IDs
                # because in round-robin polling the slot can come from any of
                # the user-selected courses, each with its own IDs.
                slot_raw_for_book = candidate.get("raw") or {}
                book_schedule_id = str(
                    slot_raw_for_book.get("schedule_id")
                    or slot_raw_for_book.get("teesheet_id")
                    or candidate.get("_source_teesheet_id")
                    or teesheet_id
                )
                book_course_id = str(slot_raw_for_book.get("course_id") or resolved_schedule_id)
                try:
                    submit_started = time.perf_counter()
                    race.detect_to_submit_ms = round((submit_started - detect_time) * 1000, 2)
                    solved_at = captcha_state.get("solved_at") or 0
                    if solved_at and (time.monotonic() - solved_at) > CAPTCHA_TOKEN_TTL:
                        race.captcha_stale_at_submit = True
                    booking_response, _booking_latency = booking.book_slot(
                        idempotency_key=f"sniper-{job_id}-{_slot_key(candidate)[0]}-{candidate.get('time', 'unknown')}",
                        slot=candidate, schedule_id=book_schedule_id,
                        course_id=book_course_id,
                        date=play_date, players=payload["players"], holes=holes,
                        request_id=request_id,
                        captcha_token=captcha_state.get("token"),
                        debug_callback=_sniper_debug,
                        customer_id=customer_id,
                        request_code_callback=request_code_callback,
                        skip_validation=True,
                    )
                    submit_ms = round((time.perf_counter() - submit_started) * 1000, 2)
                    race.submit_to_confirm_ms = submit_ms

                    # Fee-required course: we hold the slot and ForeUp will only
                    # turn it into a reservation once the fee is paid, on a
                    # hosted page we cannot drive. The race is won; the booking
                    # needs a human. Keep refreshing the hold so it survives
                    # however long that takes.
                    if booking_response.get("awaiting_payment"):
                        ttid = str(booking_response.get("pending_reservation_id", ""))
                        total = booking_response.get("booking_fee_total", 0)
                        race.record_attempt(
                            candidate.get("time", "?"), "awaiting_payment",
                            latency_ms=submit_ms, response=booking_response,
                        )
                        race.awaiting_payment = True
                        _emit_event(
                            queue, phase="checkout", stage="awaiting_payment",
                            message=(
                                f"SLOT HELD at {candidate.get('time','?')} — one step left. "
                                f"This course charges a ${total} booking fee, and ForeUp only "
                                f"creates the reservation once it is paid. Open the link below "
                                f"and pay now; the tee time stays held for you until you do. "
                                f"Nothing is booked until that payment goes through."
                            ),
                            terminal="awaiting_payment",
                            payment_url=booking_response.get("payment_url", ""),
                            booking_fee_total=total,
                            reservation_id=ttid,
                            slot=candidate,
                        )
                        queue.put({"type": "status", "status": "awaiting_payment"})

                        # The browser refreshes its hold every ~10s; do the same
                        # so a slow payment does not lose the slot we just won.
                        stop_hold = Event()

                        def _keep_hold_alive() -> None:
                            while not stop_hold.wait(8.0):
                                err = booking.refresh_pending_reservation(
                                    pending_reservation_id=ttid, request_id=request_id
                                )
                                if err:
                                    race.hold_refresh_failed += 1
                                else:
                                    race.hold_refresh_ok += 1

                        Thread(target=_keep_hold_alive, daemon=True).start()
                        booked = True
                        selected = candidate
                        break

                    # A 200 from ForeUp is not proof of a booking — it answers a
                    # rejected reservation the same way. Check the body before
                    # telling the user they have a tee time.
                    confirmed, reason = classify_reservation_response(booking_response)
                    race.booking_confirmed = confirmed
                    race.record_attempt(
                        candidate.get("time", "?"),
                        "confirmed" if confirmed else ("failed" if confirmed is False else "unconfirmed"),
                        error="" if confirmed else reason,
                        latency_ms=submit_ms,
                        response=booking_response,
                    )
                    race.booked = True
                    if confirmed:
                        _emit_event(queue, phase="checkout", stage="booking_submit",
                                    message="Booking confirmed", latency_ms=submit_ms)
                    else:
                        _emit_event(
                            queue, phase="checkout", stage="booking_unconfirmed",
                            message=(
                                f"Submitted, but ForeUp's reply does not confirm a reservation "
                                f"({reason}). VERIFY IN YOUR COURSE ACCOUNT before relying on this."
                            ),
                            latency_ms=submit_ms,
                        )
                    booked = True
                    retry_same_slot = False
                    break
                except ForeUpError as exc:
                    race.record_attempt(
                        candidate.get("time", "?"), "failed", error=str(exc),
                        latency_ms=round((time.perf_counter() - submit_started) * 1000, 2),
                    )
                    msg = str(exc).lower()
                    # 2FA-related failures: stop. The user wasn't able to enter
                    # the code in time / entered wrong code. Per product spec
                    # we don't keep racing after a missed 2FA — burned hold.
                    if "booking code was not provided" in msg or "booking code timeout" in msg:
                        race.aborted_reason = "booking_code_not_entered"
                        stop_captcha_refresh.set()
                        _emit_event(queue, phase="confirm", stage="terminal",
                                    message=f"Booking code not entered in time on slot {candidate.get('time')}. Stopping sniper.",
                                    terminal="failed", tried_slots=tried_labels + [candidate.get('time', '?')])
                        _flush_race()
                        queue.put({"type": "status", "status": "failed"})
                        _finalize_worker()
                        return
                    # Captcha invalid — re-solve and retry the SAME slot once.
                    if "captcha" in msg or "recaptcha" in msg:
                        if captcha_resolves >= CAPTCHA_RESOLVE_CAP or captcha_solver is None or not captcha_site_key:
                            _emit_event(queue, phase="captcha", stage="cap_reached",
                                        message=f"Captcha re-solve cap ({CAPTCHA_RESOLVE_CAP}) reached. Marking slot {candidate.get('time')} as tried.")
                            tried_keys.add(_slot_key(candidate))
                            tried_labels.append(candidate.get('time', '?'))
                            retry_same_slot = False
                        else:
                            try:
                                _emit_event(queue, phase="captcha", stage="resolving",
                                            message="Captcha rejected — re-solving for retry")
                                captcha_state["token"] = captcha_solver.solve(captcha_page_url, captcha_site_key, timeout=120)
                                captcha_state["solved_at"] = time.monotonic()
                                captcha_resolves += 1
                                race.captcha_refreshes += 1
                                retry_same_slot = True
                                _emit_event(queue, phase="captcha", stage="resolved",
                                            message=f"Captcha refreshed ({captcha_resolves}/{CAPTCHA_RESOLVE_CAP}) — retrying same slot")
                                continue  # skip the jitter sleep, retry immediately
                            except Exception as cap_exc:  # noqa: BLE001
                                _emit_event(queue, phase="captcha", stage="error",
                                            message=f"Captcha re-solve failed: {cap_exc} — marking slot tried")
                                tried_keys.add(_slot_key(candidate))
                                tried_labels.append(candidate.get('time', '?'))
                                retry_same_slot = False
                    else:
                        # Stale slot or unknown error — mark tried, keep polling.
                        tried_keys.add(_slot_key(candidate))
                        tried_labels.append(candidate.get('time', '?'))
                        retry_same_slot = False
                        _emit_event(queue, phase="checkout", stage="slot_taken",
                                    message=f"Slot {candidate.get('time')} unavailable ({exc}). Tried {len(tried_keys)} so far — continuing to poll.")

            elapsed = time.perf_counter() - poll_start
            if elapsed - last_status_emit >= 2.0:
                _emit_event(queue, phase="sniper", stage="poll_status",
                            message=f"Poll #{poll_count}: {len(slots)} slots ({loop_ms}ms) | tried {len(tried_keys)}",
                            latency_ms=loop_ms, tried_count=len(tried_keys))
                last_status_emit = elapsed

            jittered_interval = poll_base + _rand.uniform(0, poll_jitter)
            sleep_remaining = jittered_interval - (time.perf_counter() - loop_start)
            if sleep_remaining > 0:
                time.sleep(sleep_remaining)

        # The race is over however it ended. Stop the burst before anything
        # else: a thread still firing could win a hold we will never book, and
        # squatting on a live tee time is worse than losing it. Then hand back
        # anything it won that we did not use.
        prestage_stop.set()
        if prestage_thread is not None:
            prestage_thread.join(timeout=10)
        leftover = _take_prestage_win()
        if leftover and not booked:
            _release_hold(leftover[1])
            _emit_event(queue, phase="sniper", stage="prestage_released",
                        message=f"Released the unused pre-staged hold at {leftover[0]} "
                                f"back to the sheet.")

        total_ms = round((time.perf_counter() - poll_start) * 1000, 2)
        race.total_race_ms = total_ms
        stop_captcha_refresh.set()

        if cancelled and not booked:
            race.aborted_reason = "cancelled_by_user"
            _emit_event(queue, phase="confirm", stage="terminal",
                        message=f"Stopped by user after {poll_count} polls ({total_ms:.0f}ms). "
                                f"No booking was submitted.",
                        terminal="cancelled", tried_slots=tried_labels)
            _flush_race()
            queue.put({"type": "status", "status": "cancelled"})
            _finalize_worker()
            return

        # Held-and-awaiting-payment is not a booking, and must not be reported
        # as one. The link was already emitted; this just closes the run out
        # honestly so the history does not count it as a win.
        if booked and race.awaiting_payment:
            _flush_race()
            queue.put({"type": "status", "status": "awaiting_payment"})
            _finalize_worker()
            return

        if booked:
            _emit_event(queue, phase="confirm", stage="booking_confirmation",
                        message=(
                            "Booking confirmed"
                            if race.booking_confirmed
                            else "Submitted, but NOT confirmed by ForeUp — verify in your course account."
                        ),
                        terminal="success" if race.booking_confirmed else "unconfirmed",
                        confirmation=booking_response, slot=selected,
                        sniper_stats={"polls": poll_count, "total_ms": total_ms, "attempts": len(tried_keys) + 1})
            _flush_race()
            queue.put({"type": "status", "status": "succeeded"})
            _finalize_worker()
            return

        if not selected:
            # Distinguish "nothing was there" from "we were blocked" and from
            # "we arrived late" — these read identically without the verdict.
            _emit_event(queue, phase="confirm", stage="terminal",
                        message=f"Sniper timed out after {poll_count} polls ({total_ms}ms) — no matching slots found"
                                + (f". Tried {len(tried_keys)}: {', '.join(tried_labels)}" if tried_labels else "")
                                + f" | {race.summary_line()}",
                        terminal="failed", tried_slots=tried_labels)
            _flush_race()
            queue.put({"type": "status", "status": "failed"})
            _finalize_worker()
            return

        # Reaching here un-booked with auto-book ON means every attempt failed —
        # usually ForeUp answering "time not available" faster than we can hold
        # it. Telling the user "SLOT FOUND — book it now!" at that point sends
        # them chasing a tee time that is already gone. Alert mode (auto-book
        # off / dry run) is the only case where the alert is the product.
        if do_auto_book:
            loss_msg = f"Lost the race — tried {len(tried_keys) or 1} slot(s)"
            if tried_labels:
                loss_msg += f" ({', '.join(tried_labels)})"
            loss_msg += (
                ", each taken before our hold landed. "
                f"{poll_count} polls in {total_ms:.0f}ms. | {race.summary_line()}"
            )
            _emit_event(queue, phase="confirm", stage="terminal",
                        message=loss_msg, terminal="failed", tried_slots=tried_labels,
                        sniper_stats={"polls": poll_count, "total_ms": total_ms,
                                      "attempts": len(tried_keys) + 1,
                                      "tried_slots": tried_labels})
            _flush_race()
            queue.put({"type": "status", "status": "failed"})
            _finalize_worker()
            return

        booking_url = payload.get("foreup_booking_url") or f"{base_url}/index.php/booking/{resolved_schedule_id}/{resolved_course_id}#teetimes"
        terminal = "dry_run_stopped" if payload.get("dry_run", True) else "sniper_alert"
        alert_msg = f"Slot found: {selected['time']} | {selected['available_spots']} spots | ${selected.get('green_fee', '?')}"
        if tried_labels:
            alert_msg += f" | tried {len(tried_keys)} slots that were taken: {', '.join(tried_labels)}"
        _emit_event(queue, phase="confirm", stage="sniper_result",
                    message=alert_msg,
                    terminal=terminal, slot=selected, booking_url=booking_url,
                    sniper_stats={"polls": poll_count, "total_ms": total_ms,
                                  "attempts": len(tried_keys) + 1,
                                  "tried_slots": tried_labels})
        _flush_race()
        queue.put({"type": "status", "status": "succeeded"})
        _finalize_worker()

    except AntiBotDetectedError as exc:
        race.error = f"antibot: {exc}"
        race.polls.antibot += 1
        _emit_event(queue, phase="auth", stage="antibot", message=f"Anti-bot detected: {exc}", terminal="failed")
        _flush_race()
        queue.put({"type": "status", "status": "failed"})
        _finalize_worker()
    except ForeUpError as exc:
        race.error = str(exc)
        _emit_event(queue, phase="confirm", stage="terminal", message=f"Sniper failed: {exc}", terminal="failed")
        _flush_race()
        queue.put({"type": "status", "status": "failed"})
        _finalize_worker()
    except Exception as exc:  # noqa: BLE001
        race.error = f"{type(exc).__name__}: {exc}"
        _emit_event(queue, phase="confirm", stage="terminal", message=f"Sniper error: {exc}", terminal="failed")
        _flush_race()
        queue.put({"type": "status", "status": "failed"})
        _finalize_worker()
    finally:
        try:
            stop_captcha_refresh.set()
        except NameError:
            # Race failed before the poll loop set the refresher up.
            pass
        if captcha_solver:
            try:
                captcha_solver.close()
            except Exception:
                pass


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


def _watch_worker(job_id: str, queue: Queue, process: Process, cleanup_id: str = "") -> None:
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
    CMD_QUEUES.pop(cleanup_id, None)
    JOB_PROCS.pop(cleanup_id, None)


# --- Routes ---

APP_VERSION = "1.01"

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION}


def _warm_course_preflight() -> None:
    """Check the course links once at boot so the first visitor sees a verdict.

    Best-effort: a failure here is a logged line, not a broken app, and the UI
    re-runs the check itself if nothing is cached yet.
    """
    try:
        run_preflight(force=True)
    except Exception:  # noqa: BLE001
        _logging.getLogger(__name__).warning("course_preflight_warm_failed", exc_info=True)


@app.on_event("startup")
def _startup_preflight() -> None:
    if os.getenv("GOLFBOT_PREFLIGHT_ON_STARTUP", "1") != "1":
        return
    Thread(target=_warm_course_preflight, daemon=True).start()


@app.get("/api/course-catalog")
def course_catalog() -> dict[str, Any]:
    """The courses the UI offers, and the last verification verdict if we have one.

    Served without touching the network so the picker can render immediately;
    the verdict arrives separately from /api/course-catalog/preflight.
    """
    cached = cached_preflight()
    return {
        "courses": [c.as_dict() for c in COURSE_CATALOG],
        "preflight": cached.as_dict() if cached else None,
    }


@app.get("/api/course-catalog/preflight")
def course_preflight(refresh: bool = False) -> dict[str, Any]:
    """Confirm with ForeUp that every catalog URL still resolves.

    Runs before anyone books so a stale link is caught here rather than by an
    empty teesheet at 7pm. Cached for COURSE_PREFLIGHT_TTL_SECONDS; ``refresh=1``
    forces a fresh round of checks.
    """
    try:
        report = run_preflight(force=refresh)
    except Exception as exc:  # noqa: BLE001 - the picker must still render
        raise HTTPException(status_code=503, detail=f"Course preflight failed: {exc}")
    return report.as_dict()


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
        client.bootstrap_session(username=username, password=password, schedule_id=schedule_id, course_id=course_id, request_id=f"connect-{uuid.uuid4().hex[:8]}")
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
        "booking_classes": client.get_booking_class_options(),
        "courses": client.get_course_options(),
    }


@app.post("/api/jobs")
def start_job(req: StartJobRequest) -> dict[str, str]:
    job_id = uuid.uuid4().hex[:12]
    state = JobState(job_id=job_id)
    full_payload = req.model_dump()

    key = _racing_key(full_payload)
    with JOB_LOCK:
        JOBS[job_id] = state
        state.racing_key = key
        state.peer_ordinal, peer_count = _claim_peer_ordinal(job_id, key)
    full_payload["peer_ordinal"] = state.peer_ordinal
    full_payload["peer_count"] = peer_count

    payload = req.model_dump()
    payload.pop("password", None)
    safe_payload = {k: v for k, v in payload.items() if k != "password"}
    queue: Queue = Queue()
    cmd_queue: Queue = Queue()
    CMD_QUEUES[job_id] = cmd_queue
    worker_fn = _sniper_worker_main if full_payload.get("sniper_mode") else _worker_main
    proc = Process(target=worker_fn, args=(job_id, full_payload, queue, cmd_queue), daemon=True)
    proc.start()
    JOB_PROCS[job_id] = proc

    watcher = Thread(target=_watch_worker, args=(job_id, queue, proc, job_id), daemon=True)
    watcher.start()

    with JOB_LOCK:
        _event(JOBS[job_id], "job", "Job started", request=safe_payload)
        if peer_count > 1:
            _event(
                JOBS[job_id], "job",
                f"{peer_count} of our own jobs are racing these exact criteria. You are "
                f"#{state.peer_ordinal + 1} — your pick order is rotated so we open on "
                f"different slots instead of all fighting for the same one. Every time in "
                f"your window is still tried.",
            )

    return {"job_id": job_id}


class GridRequest(BaseModel):
    """Inputs for inferring a course's tee-time grid."""
    play_date: str
    foreup_booking_url: str | None = None
    schedule_id: str = ""
    course_id: str = ""
    # Multi-course facilities (Bethpage: Black/Red/Green/Blue/Yellow) each have
    # their own teesheet. Without these we would only ever read the one course
    # in the URL — which may be fully booked while the others have plenty.
    course_ids: list[str] = Field(default_factory=list)
    players: int = Field(default=1, ge=1, le=4)
    holes: int = Field(default=18, ge=9, le=18)
    booking_class: str = ""
    username: str = ""
    password: str = ""


@app.post("/api/teesheet-grid")
def teesheet_grid(req: GridRequest) -> dict[str, Any]:
    """Infer which tee times a course actually offers on a given date.

    A pre-staged hold has to name an exact time, so we need the course's grid
    before we can target anything: firing at 08:05 on a 10-minute sheet fails no
    matter how fast we are. Use a date with plenty of availability — the booking
    API only returns open slots, so a busy sheet hides the true interval.
    """
    default_base_url = os.getenv("FOREUP_BASE_URL", "https://foreupsoftware.com")
    base_url, schedule_id, course_id = _extract_foreup_target(
        req.foreup_booking_url,
        fallback_base_url=default_base_url,
        fallback_schedule_id=req.schedule_id or os.getenv("FOREUP_SCHEDULE_ID", ""),
        fallback_course_id=req.course_id or os.getenv("FOREUP_COURSE_ID", ""),
    )
    if not schedule_id:
        raise HTTPException(status_code=400, detail="No schedule_id — pass a ForeUp booking URL.")

    username = req.username or os.getenv("FOREUP_USERNAME", "")
    password = req.password or os.getenv("FOREUP_PASSWORD", "")

    discovery = ForeUpDiscoveryClient(base_url=base_url)
    try:
        if username and password:
            discovery.bootstrap_session(
                username=username, password=password,
                schedule_id=schedule_id, course_id=course_id,
            )

        # Read every course the user selected, not just the one in the URL. On a
        # multi-course facility the URL course is often the busiest (Bethpage
        # Black) and comes back empty while its siblings have times — which
        # looks like a broken lookup rather than a full teesheet.
        targets = [c for c in (req.course_ids or []) if c] or [course_id or schedule_id]
        per_course: list[dict[str, Any]] = []
        course_names = {
            str(c.get("teesheet_id")): c.get("name", "")
            for c in (getattr(discovery, "_course_options", None) or [])
        }

        for tid in targets:
            entry: dict[str, Any] = {"course_id": tid, "name": course_names.get(str(tid), "")}
            try:
                bc = req.booking_class
                if hasattr(discovery, "resolve_booking_class"):
                    bc = discovery.resolve_booking_class(tid, req.booking_class)
                entry["booking_class"] = bc
                found = discovery.lookup_availability(
                    schedule_id=tid, date=req.play_date,
                    players=req.players, holes=req.holes, booking_class=bc,
                )
                entry["slots"] = found
                entry["open_count"] = len(found)
                entry["grid"] = analyze_teesheet_grid(sl.get("time", "") for sl in found)
            except ForeUpError as exc:
                entry["slots"] = []
                entry["open_count"] = 0
                entry["error"] = str(exc)[:300]
                entry["grid"] = analyze_teesheet_grid([])
            per_course.append(entry)

        # Report against the first course that actually returned times, so a
        # full Black does not hide a usable Red.
        best = max(per_course, key=lambda e: e["open_count"])
        slots = best["slots"]
        course_id = best["course_id"]
    except AntiBotDetectedError as exc:
        raise HTTPException(status_code=502, detail=f"Anti-bot response: {exc}")
    except ForeUpError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # Teach the registry from every course that produced a trustworthy grid --
    # not just the best one, and not only when Future Sight happens to be
    # arming. This button is the bootstrap: until it ran, upsert_grid was only
    # reachable from the Future Sight path that had ALREADY read a live sheet,
    # so the registry could only be written when it was not needed and was
    # guaranteed empty on the day it was.
    learned: list[str] = []
    try:
        play_dow = datetime.strptime(req.play_date, "%Y-%m-%d").date().weekday()
    except ValueError:
        play_dow = None
    if play_dow is not None:
        for entry in per_course:
            eg = entry["grid"]
            if not (eg.interval_minutes and eg.consistent):
                continue
            tmpl = (entry["slots"][0].get("raw") or {}) if entry["slots"] else {}
            if grid_store.upsert_grid(course_id=str(entry["course_id"]), day_of_week=play_dow,
                                      grid=eg.as_dict(), template=tmpl,
                                      source_date=req.play_date):
                learned.append(str(entry["course_id"]))

    grid = best["grid"]
    result = grid.as_dict()
    result["learned_courses"] = learned
    result["play_date"] = req.play_date
    result["schedule_id"] = schedule_id
    result["course_id"] = course_id
    result["course_name"] = best.get("name", "")
    result["open_times"] = [s.get("time", "") for s in slots]
    result["usable_for_prestage"] = bool(
        grid.consistent and grid.confidence in ("medium", "high")
    )
    # Per-course breakdown so an empty result explains itself: which courses were
    # read, under which booking class, and how many times each returned.
    result["courses"] = [
        {
            "course_id": e["course_id"],
            "name": e.get("name", ""),
            "booking_class": e.get("booking_class", ""),
            "open_count": e["open_count"],
            "interval_minutes": e["grid"].interval_minutes,
            "error": e.get("error", ""),
        }
        for e in per_course
    ]
    return result


class ColdHoldProbeRequest(GridRequest):
    """A cold-hold probe. target_time must be a real time on the course grid."""
    target_time: str
    skip_grid_check: bool = False
    # Chain a validate_only reservation onto a successful hold. Answers whether
    # the booking-fee check runs at validation or at commit — which decides
    # whether matching the browser's request order could ever be enough.
    validate_after_hold: bool = False
    # Second leg of the validate probe. ForeUp emails a code tied to a specific
    # pending reservation, so completing the check means reusing that same hold
    # rather than taking a fresh one.
    existing_hold_id: str = ""
    booking_code: str = ""
    # Ask ForeUp for the hosted payment page after validation. Creates a
    # payment session; charges nothing on its own.
    want_payment_url: bool = False


@app.post("/api/probe/cold-hold")
def probe_cold_hold(req: ColdHoldProbeRequest) -> dict[str, Any]:
    """Test whether ForeUp will hold a tee time with no preceding poll.

    This is the pre-staging experiment. It creates a hold and stops — it never
    touches the reservations endpoint, so it cannot book. A successful hold is
    real though: that time is locked to this account until it expires (~2 min).
    Probe a slot you do not mind briefly holding.

    The target is validated against the inferred grid first, because a hold at a
    time the course does not offer would fail for that reason alone and tell us
    nothing about whether pre-staging works.
    """
    default_base_url = os.getenv("FOREUP_BASE_URL", "https://foreupsoftware.com")
    base_url, schedule_id, course_id = _extract_foreup_target(
        req.foreup_booking_url,
        fallback_base_url=default_base_url,
        fallback_schedule_id=req.schedule_id or os.getenv("FOREUP_SCHEDULE_ID", ""),
        fallback_course_id=req.course_id or os.getenv("FOREUP_COURSE_ID", ""),
    )
    if not schedule_id:
        raise HTTPException(status_code=400, detail="No schedule_id — pass a ForeUp booking URL.")

    username = req.username or os.getenv("FOREUP_USERNAME", "")
    password = req.password or os.getenv("FOREUP_PASSWORD", "")
    if not (username and password):
        raise HTTPException(status_code=400, detail="Credentials required to hold a tee time.")

    discovery = ForeUpDiscoveryClient(base_url=base_url)
    booking = ForeUpBookingClient(base_url=base_url)
    steps: list[str] = []

    try:
        discovery.bootstrap_session(
            username=username, password=password,
            schedule_id=schedule_id, course_id=course_id,
        )
        steps.append("logged in")

        # Probe the course the user actually selected. On a multi-course
        # facility the URL course is often the busiest and comes back empty.
        course_id = (
            next((c for c in (req.course_ids or []) if c), "") or course_id or schedule_id
        )
        steps.append(f"course {course_id}")

        booking_class = req.booking_class
        if hasattr(discovery, "resolve_booking_class") and course_id:
            booking_class = discovery.resolve_booking_class(course_id, req.booking_class)
        steps.append(f"booking class resolved ({booking_class or 'default'})")

        # Learn the grid, and borrow the per-course payload fields from any open
        # slot. These (side id, fee shape) are stable for a course, so at a real
        # release they would come from cache rather than a live poll.
        slots = discovery.lookup_availability(
            schedule_id=course_id, date=req.play_date,
            players=req.players, holes=req.holes, booking_class=booking_class,
        )
        if not slots:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No tee times returned for course {course_id} on {req.play_date} "
                    f"(booking class {booking_class or 'default'}). Either that date is "
                    f"not released yet or that course is full — run step 1 to see which "
                    f"courses have times."
                ),
            )
        grid = analyze_teesheet_grid(sl.get("time", "") for sl in slots)
        steps.append(
            f"grid inferred: {grid.interval_minutes}min, "
            f"{len(grid.grid_times)} times, confidence={grid.confidence}"
        )

        if not req.skip_grid_check:
            if not grid.consistent:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Refusing to probe: the sheet is not on a uniform grid "
                        f"({'; '.join(grid.notes)}). Pass skip_grid_check to override."
                    ),
                )
            # matches_interval, not is_valid_target: the latter only accepts
            # times between the first and last OPEN slot, and that window moves
            # as times get booked. A target offered by the dropdown a minute
            # ago would then be refused for being outside a span that shrank.
            if not grid.matches_interval(req.target_time):
                nearby = [t for t in grid.grid_times if t >= req.target_time][:3]
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{req.target_time} is not on this course's "
                        f"{grid.interval_minutes}min grid. Nearest real times: "
                        f"{', '.join(nearby) or 'none after that time'}."
                    ),
                )
        steps.append(f"{req.target_time} confirmed on the grid")

        # Carry the shared per-course fields; deliberately NOT the target slot's
        # own polled data, since at a release that would not exist yet.
        template = (slots[0].get("raw") or {}) if slots else {}
        if req.existing_hold_id:
            # Second leg: the code belongs to the hold we already have, and
            # taking another would strand one and invalidate the code.
            steps.append(f"reusing hold {req.existing_hold_id} — no new hold taken")
            result = {"accepted": True, "reservation_id": req.existing_hold_id,
                      "latency_ms": 0, "reused_hold": True,
                      "verdict": "Reused the hold from the first leg."}
        else:
            result = booking.probe_cold_hold(
                target_time=f"{req.play_date} {req.target_time}",
                schedule_id=str(course_id or schedule_id),
                course_id=str(schedule_id),
                players=req.players,
                holes=req.holes,
                booking_class_id=str(template.get("booking_class_id", booking_class or "")),
                teesheet_side_id=str(template.get("teesheet_side_id", "")),
                green_fee=template.get("green_fee", 0),
            )

        # Optional second step: ask ForeUp whether it WOULD take this booking.
        # Only meaningful once a hold exists, since the reservation references it.
        if req.validate_after_hold:
            hold_id = req.existing_hold_id or str(result.get("reservation_id") or "")
            if not hold_id:
                result["validate"] = {
                    "skipped": True,
                    "verdict": "No hold, so there was nothing to validate against.",
                }
            else:
                steps.append(
                    f"validating against hold {hold_id}"
                    + (" with the emailed code" if req.booking_code else "")
                )
                v = booking.probe_validate_only(
                    pending_reservation_id=hold_id,
                    target_time=f"{req.play_date} {req.target_time}",
                    template=template,
                    schedule_id=str(course_id or schedule_id),
                    course_id=str(schedule_id),
                    players=req.players,
                    holes=req.holes,
                    confirmation_code=req.booking_code or None,
                )
                # ForeUp issues the code against one specific hold, so the
                # second leg has to come back to this same one. Hand the id
                # back rather than making the caller dig it out.
                v["hold_id"] = hold_id
                if v.get("needs_code") and not req.booking_code:
                    customer_id = (
                        discovery.get_customer_id()
                        if hasattr(discovery, "get_customer_id") else None
                    )
                    if customer_id:
                        try:
                            booking.request_booking_code(
                                course_id=str(schedule_id),
                                customer_id=str(customer_id),
                                pending_reservation_id=hold_id,
                            )
                            v["code_requested"] = True
                            steps.append("booking code requested — check your email")
                        except ForeUpError as exc:
                            v["code_request_error"] = str(exc)[:400]
                    else:
                        v["code_request_error"] = "No customer id — cannot request a code."
                # Once validation passes we have everything the payment window
                # needs -- the same hold and the same code -- so ask for it.
                # This is the step our booking path has never taken.
                if req.want_payment_url and v.get("ok"):
                    steps.append("validation passed — requesting the hosted payment page")
                    result["payment"] = booking.request_booking_fee_payment(
                        facility_id=str(schedule_id),
                        teesheet_id=str(course_id or schedule_id),
                        pending_reservation_id=hold_id,
                        confirmation_code=req.booking_code,
                        target_time=f"{req.play_date} {req.target_time}",
                        players=req.players,
                        holes=req.holes,
                        booking_class_id=str(template.get("booking_class_id", booking_class or "")),
                        teesheet_side_id=str(template.get("teesheet_side_id", "")),
                    )
                result["validate"] = v
    except HTTPException:
        raise
    except AntiBotDetectedError as exc:
        raise HTTPException(status_code=502, detail=f"Anti-bot response: {exc}")
    except ForeUpError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    result["steps"] = steps
    result["target_time"] = req.target_time
    result["play_date"] = req.play_date
    result["grid"] = grid.as_dict()
    # True only if ForeUp ignored validate_only, which is the one way this path
    # can book. The UI says so explicitly rather than leaving it implied.
    result["booked_anything"] = False
    return result


@app.post("/api/probe/checkout-fields")
def probe_checkout_fields(req: GridRequest) -> dict[str, Any]:
    """Read ForeUp's own checkout script to find the fields we do not send.

    The free attempt at the booking-fee question. Fetches the booking page and
    its scripts and looks for the code that builds the reservation payload —
    our payload contains strings distinctive enough to find it — then reports
    every field name near it that we do not currently send.

    Read-only. No hold, no reservation, nothing booked.
    """
    default_base_url = os.getenv("FOREUP_BASE_URL", "https://foreupsoftware.com")
    base_url, schedule_id, course_id = _extract_foreup_target(
        req.foreup_booking_url,
        fallback_base_url=default_base_url,
        fallback_schedule_id=req.schedule_id or os.getenv("FOREUP_SCHEDULE_ID", ""),
        fallback_course_id=req.course_id or os.getenv("FOREUP_COURSE_ID", ""),
    )
    if not schedule_id:
        raise HTTPException(status_code=400, detail="No schedule_id — pick a course first.")

    discovery = ForeUpDiscoveryClient(base_url=base_url)
    username = req.username or os.getenv("FOREUP_USERNAME", "")
    password = req.password or os.getenv("FOREUP_PASSWORD", "")

    # The checkout script may only be served to a signed-in session, and logging
    # in costs nothing here — this endpoint is not on any race path.
    if username and password:
        try:
            discovery.bootstrap_session(
                username=username, password=password,
                schedule_id=schedule_id, course_id=course_id,
            )
        except ForeUpError:
            pass

    page_url = f"{base_url}/index.php/booking/{schedule_id}"

    def _fetch(url: str) -> str | None:
        try:
            resp = discovery.client.get(url, timeout=20.0)
            return resp.text if resp.status_code < 400 else None
        except Exception:  # noqa: BLE001 — a missing script is not an error
            return None

    html = _fetch(page_url)
    if not html:
        raise HTTPException(status_code=502, detail=f"Could not read {page_url}.")

    booking = ForeUpBookingClient(base_url=base_url)
    known = booking.reservation_payload_keys()

    result = checkout_scan.scan_checkout(
        html=html, base_url=base_url, page_url=page_url,
        fetch=_fetch, known_keys=known,
    )
    result["page_url"] = page_url
    result["fields_we_send"] = len(known)
    result["booked_anything"] = False
    return result


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


class VerifyCodeRequest(BaseModel):
    code: str


@app.post("/api/jobs/{job_id}/verify")
def submit_verification_code(job_id: str, req: VerifyCodeRequest) -> dict[str, str]:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status != "awaiting_input":
            raise HTTPException(status_code=400, detail=f"Job not awaiting input (status={job.status})")

    cmd_queue = CMD_QUEUES.get(job_id)
    if not cmd_queue:
        raise HTTPException(status_code=400, detail="No command queue for this job")

    cmd_queue.put({"type": "code", "code": req.code})
    return {"status": "submitted"}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, str]:
    """Stop a running job.

    Cooperative first: the worker sees the request at a safe point — never
    between opening a hold and submitting the reservation — exits cleanly, and
    still writes its race record so the run is not lost from the history. If it
    has not stopped within the grace period it is terminated.
    """
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status in {"succeeded", "failed", "cancelled"}:
            raise HTTPException(status_code=400, detail=f"Job already finished (status={job.status})")
        job.status = "cancelling"
        job.updated_at = datetime.now(timezone.utc).isoformat()
        _event(JOBS[job_id], "job", "Stop requested — finishing current step, then shutting down.")

    cmd_queue = CMD_QUEUES.get(job_id)
    if cmd_queue:
        cmd_queue.put({"type": "cancel"})

    def _hard_stop_if_needed() -> None:
        deadline = time.monotonic() + CANCEL_GRACE_SECONDS
        while time.monotonic() < deadline:
            proc = JOB_PROCS.get(job_id)
            if proc is None or not proc.is_alive():
                return
            time.sleep(0.25)

        proc = JOB_PROCS.get(job_id)
        if proc is not None and proc.is_alive():
            proc.terminate()
            with JOB_LOCK:
                stale = JOBS.get(job_id)
                if stale and stale.status not in {"succeeded", "failed", "cancelled"}:
                    stale.status = "cancelled"
                    stale.updated_at = datetime.now(timezone.utc).isoformat()
                    _event(
                        JOBS[job_id], "job",
                        f"Did not stop within {CANCEL_GRACE_SECONDS:.0f}s — process terminated. "
                        f"The race record for this run may be missing.",
                        terminal="cancelled",
                    )

    Thread(target=_hard_stop_if_needed, daemon=True).start()
    return {"status": "cancelling"}


@app.get("/")
def index() -> HTMLResponse:
    """Serve the UI with its asset URLs stamped with the running version.

    Without this, a browser can hold a cached app.js while loading fresh HTML —
    and because the version badge lives in the HTML, the page cheerfully reports
    the new version while running the old JavaScript. That is how the stop
    button shipped in v0.64 stayed invisible: the element was there, but the
    cached script never un-hid it. Stamping ?v=<version> means every deploy
    forces a re-fetch, and no-cache keeps this page itself honest.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("{{VERSION}}", APP_VERSION)
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )
