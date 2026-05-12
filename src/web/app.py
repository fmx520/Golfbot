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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.booking.foreup_client import ForeUpBookingClient
from src.config import load_config
from src.discovery.foreup_client import AntiBotDetectedError, ForeUpDiscoveryClient, ForeUpError, StaleSlotError

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
        CAPTCHA_TOKEN_TTL = 110.0

        if auto_book and anticaptcha_key:
            site_key = discovery.extract_recaptcha_sitekey(schedule_id=resolved_schedule_id)
            if site_key:
                from src.captcha.anticaptcha import AntiCaptchaSolver, CaptchaSolveError
                captcha_solver = AntiCaptchaSolver(api_key=anticaptcha_key)
                booking_page_url = payload.get("foreup_booking_url") or f"{base_url}/index.php/booking/{resolved_schedule_id}/{resolved_course_id}"
                _emit_event(queue, phase="captcha", stage="solving",
                            message=f"reCAPTCHA detected (site key: {site_key[:12]}...) — solving via Anti-Captcha")
                try:
                    captcha_started = time.perf_counter()
                    captcha_token = captcha_solver.solve(booking_page_url, site_key, timeout=120)
                    captcha_solved_at = time.monotonic()
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

        prewarm_lead = 5.0
        wait_seconds = max(0, seconds_until - prewarm_lead)
        if wait_seconds > 0:
            sleep_chunk = 2.0
            slept = 0.0
            last_update = 0.0
            while slept < wait_seconds:
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
                            booking_page_url = payload.get("foreup_booking_url") or f"{base_url}/index.php/booking/{resolved_schedule_id}/{resolved_course_id}"
                            site_key = discovery.extract_recaptcha_sitekey(schedule_id=resolved_schedule_id)
                            captcha_token = captcha_solver.solve(booking_page_url, site_key, timeout=120)
                            captcha_solved_at = time.monotonic()
                            _emit_event(queue, phase="captcha", stage="solved", message="Fresh CAPTCHA token cached")
                        except Exception:
                            _emit_event(queue, phase="captcha", stage="failed", message="CAPTCHA refresh failed — will use expired token")

        rewarm_started = time.perf_counter()
        if username and password:
            discovery.refresh_session(
                schedule_id=resolved_schedule_id, course_id=resolved_course_id,
                username=username, password=password, request_id=request_id,
            )
            _emit_event(queue, phase="auth", stage="rewarm",
                        message="Session re-warmed",
                        latency_ms=round((time.perf_counter() - rewarm_started) * 1000, 2))

        remaining = (release_dt - datetime.now(EAST_COAST_TZ)).total_seconds()
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
        selected = None

        while time.perf_counter() - poll_start < timeout:
            loop_start = time.perf_counter()
            poll_count += 1

            try:
                slots = discovery.lookup_availability_fast(
                    schedule_id=teesheet_id,
                    date=play_date,
                    players=payload["players"],
                    holes=holes,
                    booking_class=payload.get("booking_class", ""),
                    request_id=f"{request_id}-p{poll_count}",
                )
            except ForeUpError:
                slots = []

            loop_ms = round((time.perf_counter() - loop_start) * 1000, 1)

            if slots:
                selected = _select_slot(slots, payload)
                if selected:
                    _emit_event(queue, phase="sniper", stage="found",
                                message=f"Poll #{poll_count}: {len(slots)} slots — TARGET FOUND: {selected['time']} ({selected['available_spots']} spots, ${selected.get('green_fee', '?')})",
                                latency_ms=loop_ms, slot=selected)
                    break

            elapsed = time.perf_counter() - poll_start
            if elapsed - last_status_emit >= 2.0:
                _emit_event(queue, phase="sniper", stage="poll_status",
                            message=f"Poll #{poll_count}: {len(slots)} slots ({loop_ms}ms)",
                            latency_ms=loop_ms)
                last_status_emit = elapsed

            jittered_interval = poll_base + _rand.uniform(0, poll_jitter)
            sleep_remaining = jittered_interval - (time.perf_counter() - loop_start)
            if sleep_remaining > 0:
                time.sleep(sleep_remaining)

        total_ms = round((time.perf_counter() - poll_start) * 1000, 2)

        if not selected:
            _emit_event(queue, phase="confirm", stage="terminal",
                        message=f"Sniper timed out after {poll_count} polls ({total_ms}ms) — no matching slots found",
                        terminal="failed")
            queue.put({"type": "status", "status": "failed"})
            _finalize_worker()
            return

        if auto_book and not payload.get("dry_run", True):
            try:
                booking = ForeUpBookingClient(base_url=base_url)
                if username and password:
                    booking.bootstrap_session(username=username, password=password,
                                              schedule_id=resolved_schedule_id, course_id=resolved_course_id,
                                              request_id=request_id)
                submit_started = time.perf_counter()
                token_msg = " (with CAPTCHA token)" if captcha_token else ""
                def _sniper_debug(event: str, data: dict[str, Any]) -> None:
                    _emit_event(queue, phase="checkout", stage=f"debug_{event}",
                                message=f"{event}: {data}")

                booking_response, booking_latency = booking.book_slot(
                    idempotency_key=f"sniper-{job_id}-{selected.get('tee_time_id', 'unknown')}",
                    slot=selected, schedule_id=teesheet_id,
                    course_id=resolved_schedule_id,
                    date=play_date, players=payload["players"], holes=holes,
                    request_id=request_id,
                    captcha_token=captcha_token,
                    debug_callback=_sniper_debug,
                    # Sniper just observed this slot in lookup_availability_fast a
                    # few hundred ms ago — skip the GET pre-flight and shave a
                    # full RTT off the time-to-first-POST in the race.
                    skip_validation=True,
                )
                _emit_event(queue, phase="checkout", stage="booking_submit",
                            message=f"Booking submitted{token_msg}",
                            latency_ms=round((time.perf_counter() - submit_started) * 1000, 2))

                resp_str = str(booking_response).lower()
                needs_code = (
                    booking_response.get("requires_code")
                    or "booking_code" in resp_str
                    or "verification" in resp_str
                    or "booking code" in resp_str
                )

                if needs_code and cmd_queue is not None:
                    reservation_id = booking_response.get("reservation_id", booking_response.get("id", ""))
                    _emit_event(queue, phase="verify", stage="awaiting_code",
                                message=f"Verification code sent to your email — enter it below (5 min timeout). Response: {booking_response}",
                                terminal="awaiting_code")
                    queue.put({"type": "status", "status": "awaiting_input"})
                    try:
                        cmd = cmd_queue.get(timeout=300)
                        code = cmd.get("code", "")
                        _emit_event(queue, phase="verify", stage="submitting_code", message=f"Submitting verification code: {code}")
                        queue.put({"type": "status", "status": "running"})
                        verify_response = booking.submit_booking_code(
                            code=code, reservation_id=str(reservation_id),
                            schedule_id=teesheet_id, request_id=request_id,
                        )
                        _emit_event(queue, phase="confirm", stage="booking_confirmation",
                                    message=f"Verification submitted — response: {verify_response}",
                                    terminal="success", confirmation=verify_response, slot=selected)
                        queue.put({"type": "status", "status": "succeeded"})
                    except Empty:
                        _emit_event(queue, phase="confirm", stage="terminal",
                                    message="Verification code timeout — slot hold expired", terminal="failed")
                        queue.put({"type": "status", "status": "failed"})
                    _finalize_worker()
                    return

                _emit_event(queue, phase="confirm", stage="booking_confirmation",
                            message="Booking confirmed", terminal="success",
                            confirmation=booking_response, slot=selected)
                queue.put({"type": "status", "status": "succeeded"})
                _finalize_worker()
                return
            except ForeUpError as exc:
                _emit_event(queue, phase="checkout", stage="booking_failed",
                            message=f"Auto-book failed: {exc} — slot details shown below")

        booking_url = payload.get("foreup_booking_url") or f"{base_url}/index.php/booking/{resolved_schedule_id}/{resolved_course_id}#teetimes"
        terminal = "dry_run_stopped" if payload.get("dry_run", True) else "sniper_alert"
        _emit_event(queue, phase="confirm", stage="sniper_result",
                    message=f"Slot found: {selected['time']} | {selected['available_spots']} spots | ${selected.get('green_fee', '?')}",
                    terminal=terminal, slot=selected, booking_url=booking_url,
                    sniper_stats={"polls": poll_count, "total_ms": total_ms})
        queue.put({"type": "status", "status": "succeeded"})
        _finalize_worker()

    except AntiBotDetectedError as exc:
        _emit_event(queue, phase="auth", stage="antibot", message=f"Anti-bot detected: {exc}", terminal="failed")
        queue.put({"type": "status", "status": "failed"})
        _finalize_worker()
    except ForeUpError as exc:
        _emit_event(queue, phase="confirm", stage="terminal", message=f"Sniper failed: {exc}", terminal="failed")
        queue.put({"type": "status", "status": "failed"})
        _finalize_worker()
    except Exception as exc:  # noqa: BLE001
        _emit_event(queue, phase="confirm", stage="terminal", message=f"Sniper error: {exc}", terminal="failed")
        queue.put({"type": "status", "status": "failed"})
        _finalize_worker()
    finally:
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


# --- Routes ---

APP_VERSION = "0.57"

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


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
