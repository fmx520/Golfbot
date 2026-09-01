"""Core logic for learning every course's tee-time grid in one pass.

Extracted from ``scripts/harvest_grids.py`` so the web app can run the exact
same code the CLI does, on demand, from the "Refresh tee-grid coverage"
button — one implementation, two front ends (CLI and HTTP), instead of the web
endpoint growing its own copy that quietly drifts from the script.

See ``scripts/harvest_grids.py``'s module docstring for *why* one run already
covers every weekday, and ``README.md``'s "Learning every course's tee grid"
section for the full picture.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from src.courses import Course
from src.discovery.foreup_client import ForeUpDiscoveryClient, ForeUpError
from src.discovery.grid import analyze_teesheet_grid
from src.telemetry import grid_store

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Being a background maintenance pass rather than a race, there is no reason to
# hit ForeUp back-to-back — a short pause between reads keeps this looking like
# a person browsing dates, not a burst of automated requests.
REQUEST_PAUSE_SECONDS = 0.5


@dataclass
class TeesheetTarget:
    """One thing to learn a 7-day grid for: a specific ForeUp teesheet."""

    course_key: str       # catalog key, e.g. "bethpage" — for display/filtering
    course_name: str      # catalog display name
    teesheet_id: str      # what actually keys course_grids rows
    teesheet_name: str    # e.g. "Black Course" — may differ from course_name
    booking_class: str    # resolved once from the teesheet's own class list


@dataclass
class DayResult:
    day_of_week: int
    date: str
    status: str            # stored | unconfirmed | error
    detail: str
    interval_minutes: Optional[int] = None
    confidence: str = ""

    def as_dict(self) -> dict:
        return {
            "day_of_week": self.day_of_week,
            "weekday": WEEKDAY_NAMES[self.day_of_week],
            "date": self.date,
            "status": self.status,
            "detail": self.detail,
            "interval_minutes": self.interval_minutes,
            "confidence": self.confidence,
        }


@dataclass
class TargetResult:
    target: TeesheetTarget
    days: list[DayResult] = field(default_factory=list)

    @property
    def stored_count(self) -> int:
        return sum(1 for d in self.days if d.status == "stored")

    def as_dict(self) -> dict:
        return {
            "course_key": self.target.course_key,
            "course_name": self.target.course_name,
            "teesheet_id": self.target.teesheet_id,
            "teesheet_name": self.target.teesheet_name,
            "stored_count": self.stored_count,
            "days": [d.as_dict() for d in self.days],
        }


def weekday_dates(start: date, horizon_days: int) -> dict[int, date]:
    """The first date >= start for each day-of-week, within the horizon.

    This is the whole trick: on any given day, the next `horizon_days` dates
    already contain one instance of every weekday (as long as horizon >= 7),
    because they're consecutive. No need to come back on seven different days.
    """
    out: dict[int, date] = {}
    for offset in range(horizon_days):
        d = start + timedelta(days=offset)
        out.setdefault(d.weekday(), d)
    return out


def discover_targets(
    course: Course, *, username: str, password: str, base_url_override: str = ""
) -> tuple[Optional[ForeUpDiscoveryClient], list[TeesheetTarget], str]:
    """Bootstrap one session per catalog course and enumerate its teesheets.

    One page load covers every sibling teesheet on a multi-course facility
    (Bethpage's Black/Red/Blue/Green/Yellow), so this is one login per catalog
    entry, not one per teesheet.
    """
    base_url = base_url_override or course.base_url
    client = ForeUpDiscoveryClient(base_url=base_url)
    try:
        client.bootstrap_session(
            username=username,
            password=password,
            schedule_id=course.schedule_id,
            course_id=course.course_id,
            request_id=f"harvest-{course.key}",
        )
    except ForeUpError as exc:
        return None, [], f"Login failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, [], f"Could not reach ForeUp ({type(exc).__name__}: {exc})"

    options = client.get_course_options()
    if not options:
        # Single-course facility, or the SCHEDULES array didn't parse — fall
        # back to the one teesheet the catalog URL names.
        options = [{"teesheet_id": course.course_id, "name": course.name, "classes": []}]

    targets = []
    for opt in options:
        tid = str(opt.get("teesheet_id") or "")
        if not tid:
            continue
        classes = opt.get("classes") or []
        booking_class = str(classes[0]["id"]) if classes else ""
        targets.append(TeesheetTarget(
            course_key=course.key,
            course_name=course.name,
            teesheet_id=tid,
            teesheet_name=opt.get("name") or tid,
            booking_class=booking_class,
        ))
    return client, targets, ""


def harvest_target(
    client: ForeUpDiscoveryClient,
    target: TeesheetTarget,
    *,
    weekdays: dict[int, date],
    players: int,
    holes: int,
    dry_run: bool,
) -> TargetResult:
    result = TargetResult(target=target)
    for dow in range(7):
        d = weekdays.get(dow)
        if d is None:
            result.days.append(DayResult(dow, "", "error", "No date in the horizon for this weekday."))
            continue
        date_str = d.isoformat()
        try:
            slots = client.lookup_availability(
                schedule_id=target.teesheet_id,
                date=date_str,
                players=players,
                holes=holes,
                booking_class=target.booking_class,
                request_id=f"harvest-{target.teesheet_id}-{date_str}",
            )
        except ForeUpError as exc:
            result.days.append(DayResult(dow, date_str, "error", str(exc)))
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        grid = analyze_teesheet_grid(sl.get("time", "") for sl in slots)
        if not grid.interval_minutes or not grid.consistent:
            reason = grid.notes[0] if grid.notes else f"only {grid.observed_count} open slot(s)"
            result.days.append(DayResult(dow, date_str, "unconfirmed", reason))
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        detail = f"{grid.interval_minutes}min grid, {grid.observed_count} slots, {grid.first_time}–{grid.last_time}"
        if dry_run:
            result.days.append(DayResult(dow, date_str, "stored", detail + " (dry run — not written)",
                                          interval_minutes=grid.interval_minutes, confidence=grid.confidence))
        else:
            template = (slots[0].get("raw") or {}) if slots else {}
            ok = grid_store.upsert_grid(
                course_id=target.teesheet_id,
                day_of_week=dow,
                grid=grid.as_dict(),
                template=template,
                source_date=date_str,
            )
            status = "stored" if ok else "error"
            detail = detail if ok else detail + " — write failed, see GOLFBOT_GRID_STORE log"
            result.days.append(DayResult(dow, date_str, status, detail,
                                          interval_minutes=grid.interval_minutes, confidence=grid.confidence))
        time.sleep(REQUEST_PAUSE_SECONDS)
    return result
