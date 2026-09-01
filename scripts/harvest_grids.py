#!/usr/bin/env python3
"""Learn every course's tee-time grid for every day of the week, in one run.

Future Sight can only pre-stage a hold at a time the course actually offers,
and the only way to know that ahead of a release is to have already read a
visible teesheet for that (course, day-of-week) pair. Today that happens as a
side effect of arming a real Future Sight job — which is exactly why it looks
like learning "Saturday" requires waiting for an actual Saturday.

It doesn't. These courses release on a rolling window (the FAQ already says
"many NY state courses release at 7:00 PM for the following week"), so the
booking window visible *right now* already spans every weekday exactly once:
today, tomorrow, +2, ... +6 are seven consecutive dates, so they cover Monday
through Sunday in a single pass. This script reads those dates for every
course (and every sibling teesheet on a multi-course facility like Bethpage)
and upserts what it learns into the same course_grids registry Future Sight
reads from at arm time — see src/telemetry/grid_store.py and
supabase/schema.sql.

Safe to re-run: rows are upserted by (course_id, day_of_week), so running this
weekly (a date that came back booked solid, or an interval that shifted with
the season) just refreshes what's there. A course with a shorter release
window than 7 days needs a couple of runs on different days before every
weekday has been visible at least once — --horizon-days gives it slack, but a
short window still means today's run cannot see next Saturday until the
window rolls that far.

    python3 scripts/harvest_grids.py
    python3 scripts/harvest_grids.py --course bethpage --horizon-days 13
    python3 scripts/harvest_grids.py --dry-run --json

Requires FOREUP_USERNAME / FOREUP_PASSWORD (or --username/--password) and
RACE_LOG_WEBHOOK_URL (Supabase) to actually persist anything; without a sink
this runs in an implicit dry-run and just reports what it would have stored.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.courses import COURSE_CATALOG, Course  # noqa: E402
from src.discovery.foreup_client import ForeUpDiscoveryClient, ForeUpError  # noqa: E402
from src.discovery.grid import analyze_teesheet_grid  # noqa: E402
from src.telemetry import grid_store  # noqa: E402

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


@dataclass
class TargetResult:
    target: TeesheetTarget
    days: list[DayResult] = field(default_factory=list)

    @property
    def stored_count(self) -> int:
        return sum(1 for d in self.days if d.status == "stored")


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--course", action="append", metavar="KEY",
                         help="Catalog course key to harvest (repeatable). Default: all.")
    parser.add_argument("--horizon-days", type=int, default=10,
                         help="How many consecutive dates ahead to scan for one-per-weekday coverage "
                              "(default 10 — a little slack past the usual 7-day window).")
    parser.add_argument("--players", type=int, default=1, help="Fewer players show more open slots (default 1).")
    parser.add_argument("--holes", type=int, default=18, choices=(9, 18))
    parser.add_argument("--username", default=os.getenv("FOREUP_USERNAME", ""))
    parser.add_argument("--password", default=os.getenv("FOREUP_PASSWORD", ""))
    parser.add_argument("--dry-run", action="store_true", help="Print what would be learned; don't write.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output instead of a table.")
    args = parser.parse_args()

    if not args.username or not args.password:
        parser.error("ForeUp credentials required: --username/--password, or FOREUP_USERNAME/FOREUP_PASSWORD.")

    dry_run = args.dry_run
    if not dry_run and not os.getenv("RACE_LOG_WEBHOOK_URL", "").strip():
        print("No RACE_LOG_WEBHOOK_URL set — nothing can be persisted. Running as --dry-run.\n", file=sys.stderr)
        dry_run = True

    wanted = set(args.course) if args.course else None
    courses = [c for c in COURSE_CATALOG if not wanted or c.key in wanted]
    if not courses:
        parser.error(f"No catalog course matches {sorted(wanted or [])}.")

    today = date.today()
    weekdays = weekday_dates(today, max(7, args.horizon_days))

    results: list[TargetResult] = []
    login_failures: list[tuple[str, str]] = []

    for course in courses:
        client, targets, err = discover_targets(course, username=args.username, password=args.password)
        if err:
            login_failures.append((course.name, err))
            continue
        for target in targets:
            results.append(harvest_target(
                client, target, weekdays=weekdays, players=args.players, holes=args.holes, dry_run=dry_run,
            ))

    if args.json:
        print(json.dumps({
            "checked_at": today.isoformat(),
            "dry_run": dry_run,
            "login_failures": [{"course": n, "error": e} for n, e in login_failures],
            "targets": [
                {
                    "course_key": r.target.course_key,
                    "course_name": r.target.course_name,
                    "teesheet_id": r.target.teesheet_id,
                    "teesheet_name": r.target.teesheet_name,
                    "stored_count": r.stored_count,
                    "days": [
                        {
                            "day_of_week": d.day_of_week,
                            "weekday": WEEKDAY_NAMES[d.day_of_week],
                            "date": d.date,
                            "status": d.status,
                            "detail": d.detail,
                            "interval_minutes": d.interval_minutes,
                            "confidence": d.confidence,
                        }
                        for d in r.days
                    ],
                }
                for r in results
            ],
        }, indent=2))
    else:
        print(f"Harvesting grids for {len(courses)} course(s), {len(results)} teesheet(s) "
              f"{'(dry run)' if dry_run else ''}\n")
        for name, err in login_failures:
            print(f"  [SKIP] {name}: {err}")
        for r in results:
            print(f"{r.target.course_name} — {r.target.teesheet_name} ({r.target.teesheet_id})")
            for d in r.days:
                mark = {"stored": "OK", "unconfirmed": "?? ", "error": "ERR"}[d.status]
                interval = f"{d.interval_minutes}min" if d.interval_minutes else "-"
                print(f"    [{mark}] {WEEKDAY_NAMES[d.day_of_week]} {d.date or '(no date)':<10} "
                      f"{interval:<7} {d.detail}")
            print(f"    {r.stored_count}/7 days learned\n")

    total_days = len(results) * 7
    stored_days = sum(r.stored_count for r in results)
    if not args.json:
        print(f"{stored_days}/{total_days} (course, weekday) pairs learned this run.")
        if stored_days < total_days:
            print("Unlearned pairs were booked solid, irregular, or errored — safe to re-run; "
                  "the window rolls forward daily so a later run gets another date for them.")

    if login_failures:
        return 2
    return 0 if stored_days == total_days else 1 if stored_days > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
