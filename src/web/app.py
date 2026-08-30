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

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# All courses we target are East Coast — anchor sniper timing to NY tz so the
# bot fires at the right wall-clock minute regardless of where the server is
# deployed (Render runs containers in UTC by default).
EAST_COAST_TZ = ZoneInfo("America/New_York")
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
from src.discovery.foreup_client import AntiBotDetectedError, ForeUpDiscoveryClient, ForeUpError, StaleSlotError
from src.telemetry.race_log import RaceRecorder

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
    booking_class: str = ""
    sniper_mode: bool = False
    release_time: str = ""
    auto_book: bool = False
    anticaptcha_api_key: str = ""
    course_ids: list[str] = Field(default_factory=list)
    ranking: str = "earliest"
    preselected_slot: dict[str, Any] | None = None


@dataclass
class JobState:
    job_id: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    events: list[dict[str, Any]] = field(default_factory=list)


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
    return candidates[0]


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
    )

    def _flush_race() -> None:
        """Emit the race record and surface the one-line verdict in the UI."""
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
        timeout = 90.0
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

        while time.perf_counter() - poll_start < timeout:
            # Top of the loop is the safe point: no hold is open and no booking
            # is in flight, so stopping here cannot strand anything.
            if _cancel_requested():
                cancelled = True
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

APP_VERSION = "0.69"

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION}


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
    with JOB_LOCK:
        JOBS[job_id] = state

    payload = req.model_dump()
    payload.pop("password", None)
    safe_payload = {k: v for k, v in payload.items() if k != "password"}

    full_payload = req.model_dump()
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
