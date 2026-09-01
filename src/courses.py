"""Curated ForeUp course catalog, plus a preflight that proves each link still works.

Pasting a booking URL by hand is the one step in the flow with no feedback loop:
a stale or mistyped link looks identical to a good one right up until the bot
searches an empty teesheet, or races a release on the wrong course. So the four
courses we support are pinned here as the single source of truth, and
``run_preflight`` checks them against ForeUp itself before anyone books.

The check is deliberately cheap and unauthenticated — one GET of the public
booking page per course — because it runs on app start and on every visit to
step 1. It answers exactly one question: *does ForeUp still recognise the
schedule and course ids in this URL?*
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

from src.discovery.foreup_client import ANTIBOT_MARKERS, BROWSER_HEADERS, extract_schedules

logger = logging.getLogger(__name__)

# A fetcher takes a URL and returns (http_status, body). Injectable so the
# preflight can be exercised without touching the network.
Fetcher = Callable[[str], "tuple[int, str]"]

# Verification verdicts, worst-to-best in terms of what they let you conclude.
STATUS_OK = "ok"                    # ForeUp recognises both ids in the URL
STATUS_MISMATCH = "mismatch"        # page loaded, but the course id is not on it
STATUS_UNVERIFIED = "unverified"    # page loaded, but we could not read it (anti-bot?)
STATUS_UNREACHABLE = "unreachable"  # ForeUp did not answer, or answered non-200
STATUS_MALFORMED = "malformed"      # the catalog entry itself is not a booking URL

# Only MISMATCH and MALFORMED are positive evidence that a URL is wrong; the
# other two failures mean "could not tell", which is a caution, not a block.
CONCLUSIVELY_BAD = frozenset({STATUS_MISMATCH, STATUS_MALFORMED})

PREFLIGHT_TTL_SECONDS = float(os.getenv("COURSE_PREFLIGHT_TTL_SECONDS", "900"))
PREFLIGHT_TIMEOUT_SECONDS = float(os.getenv("COURSE_PREFLIGHT_TIMEOUT_SECONDS", "10"))


def split_booking_url(url: str) -> tuple[str, str, str]:
    """Split a ForeUp booking URL into (base_url, schedule_id, course_id).

    Returns empty ids when the URL is not of the /index.php/booking/<sched>/<course>
    shape. The fragment (``#/teetimes``) and any query string are ignored.
    """
    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return "", "", ""
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    parts = [p for p in parsed.path.split("/") if p]
    if "booking" not in parts:
        return base_url, "", ""
    rest = parts[parts.index("booking") + 1:]
    schedule_id = rest[0] if len(rest) >= 1 else ""
    course_id = rest[1] if len(rest) >= 2 else ""
    return base_url, schedule_id, course_id


@dataclass(frozen=True)
class Course:
    """One bookable course, keyed by the ForeUp URL we know works for it."""

    key: str
    name: str
    emoji: str
    booking_url: str
    note: str = ""

    @property
    def base_url(self) -> str:
        return split_booking_url(self.booking_url)[0]

    @property
    def schedule_id(self) -> str:
        return split_booking_url(self.booking_url)[1]

    @property
    def course_id(self) -> str:
        return split_booking_url(self.booking_url)[2]

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "name": self.name,
            "emoji": self.emoji,
            "booking_url": self.booking_url,
            "base_url": self.base_url,
            "schedule_id": self.schedule_id,
            "course_id": self.course_id,
            "note": self.note,
        }


# The supported courses. Every id the app uses is derived from these URLs, so
# fixing a link here fixes it everywhere — UI dropdown, jobs, sniper, grid probe.
COURSE_CATALOG: tuple[Course, ...] = (
    Course(
        key="bethpage",
        name="Bethpage State Park",
        emoji="⛳",
        booking_url="https://foreupsoftware.com/index.php/booking/19765/2431#/teetimes",
        note="Multiple courses and tee-time variants share this booking page: Black, Red, "
             "Blue, Green, Yellow, plus 9-hole and early-AM options.",
    ),
    Course(
        key="montauk_downs",
        name="Montauk Downs State Park",
        emoji="🦞",
        booking_url="https://foreupsoftware.com/index.php/booking/19756/2436#/teetimes",
    ),
    Course(
        key="sunken_meadow",
        name="Sunken Meadow State Park",
        emoji="🌊",
        booking_url="https://foreupsoftware.com/index.php/booking/19766/2437#/teetimes",
    ),
    Course(
        key="crab_meadow",
        name="Crab Meadow Golf Course",
        emoji="🦀",
        booking_url="https://foreupsoftware.com/index.php/booking/21593/8314#/teetimes",
    ),
)

COURSES_BY_KEY: dict[str, Course] = {c.key: c for c in COURSE_CATALOG}


@dataclass(frozen=True)
class CourseCheck:
    """What we learned about one course's URL."""

    key: str
    name: str
    booking_url: str
    schedule_id: str
    course_id: str
    status: str
    detail: str
    foreup_name: str = ""
    latency_ms: float = 0.0
    teesheets: tuple[dict[str, str], ...] = ()

    @property
    def verified(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "booking_url": self.booking_url,
            "schedule_id": self.schedule_id,
            "course_id": self.course_id,
            "status": self.status,
            "detail": self.detail,
            "foreup_name": self.foreup_name,
            "latency_ms": round(self.latency_ms, 1),
            "teesheets": [dict(t) for t in self.teesheets],
            "verified": self.verified,
        }


@dataclass(frozen=True)
class PreflightReport:
    checked_at: str
    duration_ms: float
    checks: tuple[CourseCheck, ...]
    cached: bool = False

    @property
    def ok_count(self) -> int:
        return sum(1 for c in self.checks if c.verified)

    @property
    def all_ok(self) -> bool:
        return bool(self.checks) and self.ok_count == len(self.checks)

    @property
    def any_bad(self) -> bool:
        return any(c.status in CONCLUSIVELY_BAD for c in self.checks)

    @property
    def summary(self) -> str:
        total = len(self.checks)
        if self.all_ok:
            plural = "s" if total != 1 else ""
            return f"All {total} course link{plural} verified with ForeUp."
        bad = [c for c in self.checks if c.status in CONCLUSIVELY_BAD]
        if bad:
            names = ", ".join(c.name for c in bad)
            return f"{self.ok_count} of {total} verified — wrong link for: {names}."
        return (
            f"{self.ok_count} of {total} verified — ForeUp did not answer for the rest. "
            "The links look right, we just could not confirm them."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "duration_ms": round(self.duration_ms, 1),
            "cached": self.cached,
            "ok_count": self.ok_count,
            "total": len(self.checks),
            "all_ok": self.all_ok,
            "any_bad": self.any_bad,
            "summary": self.summary,
            "courses": [c.as_dict() for c in self.checks],
        }


def _default_fetch(url: str) -> tuple[int, str]:
    with httpx.Client(
        timeout=httpx.Timeout(PREFLIGHT_TIMEOUT_SECONDS),
        follow_redirects=True,
        headers=BROWSER_HEADERS,
    ) as client:
        response = client.get(url)
        return response.status_code, response.text


def _teesheet_name(sched: dict[str, Any]) -> str:
    return str(
        sched.get("title")
        or sched.get("schedule_name")
        or sched.get("name")
        or f"Course {sched.get('teesheet_id', '')}"
    )


def check_course(course: Course, *, fetch: Optional[Fetcher] = None) -> CourseCheck:
    """Confirm ForeUp still recognises the schedule and course ids in one URL.

    Never raises: a preflight that blows up is less useful than one that reports
    "could not tell", because the second still lets the user proceed knowingly.
    """
    fetch = fetch or _default_fetch
    base_url, schedule_id, course_id = split_booking_url(course.booking_url)

    def result(status: str, detail: str, **kw: Any) -> CourseCheck:
        return CourseCheck(
            key=course.key,
            name=course.name,
            booking_url=course.booking_url,
            schedule_id=schedule_id,
            course_id=course_id,
            status=status,
            detail=detail,
            **kw,
        )

    if not base_url or not schedule_id or not course_id:
        return result(
            STATUS_MALFORMED,
            "Not a ForeUp booking URL — expected .../index.php/booking/<schedule>/<course>.",
        )

    started = time.perf_counter()
    try:
        status_code, body = fetch(f"{base_url}/index.php/booking/{schedule_id}")
    except Exception as exc:  # noqa: BLE001 - any transport failure is "could not tell"
        elapsed = (time.perf_counter() - started) * 1000
        return result(
            STATUS_UNREACHABLE,
            f"Could not reach ForeUp ({type(exc).__name__}).",
            latency_ms=elapsed,
        )
    elapsed = (time.perf_counter() - started) * 1000

    if status_code != 200:
        return result(
            STATUS_UNREACHABLE,
            f"ForeUp returned HTTP {status_code} for schedule {schedule_id}.",
            latency_ms=elapsed,
        )

    schedules = extract_schedules(body)
    if not schedules:
        lowered = body[:5000].lower()
        blocker = next((m for m in ANTIBOT_MARKERS if m in lowered), "")
        detail = (
            f"Page loaded but is showing a bot check ({blocker}), so the ids could not be read."
            if blocker
            else "Page loaded but ForeUp's schedule list was not in it — the page layout may have changed."
        )
        return result(STATUS_UNVERIFIED, detail, latency_ms=elapsed)

    teesheets = tuple(
        {"id": str(s.get("teesheet_id", "")), "name": _teesheet_name(s)}
        for s in schedules
        if s.get("teesheet_id")
    )

    for sched in schedules:
        if str(sched.get("teesheet_id", "")) == course_id:
            return result(
                STATUS_OK,
                f"ForeUp resolves this link to “{_teesheet_name(sched)}”.",
                foreup_name=_teesheet_name(sched),
                latency_ms=elapsed,
                teesheets=teesheets,
            )

    # Some ForeUp links carry a booking class id in the second slot rather than a
    # teesheet id. That is still a working link, so say so rather than crying wolf.
    for sched in schedules:
        for bc in sched.get("booking_classes") or []:
            if str(bc.get("booking_class_id", "")) == course_id:
                label = bc.get("name") or bc.get("booking_class_name") or course_id
                return result(
                    STATUS_OK,
                    f"ForeUp resolves this link to the “{label}” booking class on “{_teesheet_name(sched)}”.",
                    foreup_name=_teesheet_name(sched),
                    latency_ms=elapsed,
                    teesheets=teesheets,
                )

    known = ", ".join(f"{t['name']} ({t['id']})" for t in teesheets) or "none"
    return result(
        STATUS_MISMATCH,
        f"Schedule {schedule_id} loaded, but it has no course {course_id}. It offers: {known}.",
        latency_ms=elapsed,
        teesheets=teesheets,
    )


_cache_lock = Lock()
_cached_report: Optional[PreflightReport] = None
_cached_at: float = 0.0


def run_preflight(
    *,
    force: bool = False,
    fetch: Optional[Fetcher] = None,
    courses: Optional[tuple[Course, ...]] = None,
) -> PreflightReport:
    """Check every catalog URL, in parallel, with a short-lived cache.

    Every page load of step 1 asks for this, so without the cache four
    unauthenticated GETs would go to ForeUp per visitor per refresh — noise we do
    not want to make against a host that is already watching for bots.
    """
    global _cached_report, _cached_at

    targets = courses if courses is not None else COURSE_CATALOG

    if not force and courses is None and fetch is None:
        cached = cached_preflight()
        if cached is not None:
            return cached

    started = time.perf_counter()
    if targets:
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            checks = tuple(pool.map(lambda c: check_course(c, fetch=fetch), targets))
    else:
        checks = ()
    report = PreflightReport(
        checked_at=datetime.now(timezone.utc).isoformat(),
        duration_ms=(time.perf_counter() - started) * 1000,
        checks=checks,
    )

    logger.info(
        "course_preflight",
        extra={
            "ok_count": report.ok_count,
            "total": len(report.checks),
            "all_ok": report.all_ok,
            "duration_ms": round(report.duration_ms, 1),
            "statuses": {c.key: c.status for c in report.checks},
        },
    )

    if courses is None and fetch is None:
        with _cache_lock:
            _cached_report = report
            _cached_at = time.monotonic()

    return report


def cached_preflight() -> Optional[PreflightReport]:
    """The last report, if one is still fresh. Used to avoid blocking callers."""
    with _cache_lock:
        if _cached_report is not None and (time.monotonic() - _cached_at) < PREFLIGHT_TTL_SECONDS:
            return PreflightReport(
                checked_at=_cached_report.checked_at,
                duration_ms=_cached_report.duration_ms,
                checks=_cached_report.checks,
                cached=True,
            )
    return None
