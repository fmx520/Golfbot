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

Also runs from the Golfbot web UI itself ("Refresh Tee-Grid Coverage" on the
Find Tee Times step, using whichever ForeUp account is currently connected)
and weekly via .github/workflows/harvest-grids.yml. All three call the same
code in src/telemetry/grid_harvest.py — this file is just the CLI front end.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.courses import COURSE_CATALOG  # noqa: E402
from src.telemetry.grid_harvest import (  # noqa: E402
    WEEKDAY_NAMES,
    TargetResult,
    discover_targets,
    harvest_target,
    weekday_dates,
)


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
    notes: list[tuple[str, str]] = []

    for course in courses:
        client, targets, err, note = discover_targets(course, username=args.username, password=args.password)
        if err:
            login_failures.append((course.name, err))
            continue
        if note:
            notes.append((course.name, note))
        for target in targets:
            results.append(harvest_target(
                client, target, weekdays=weekdays, players=args.players, holes=args.holes, dry_run=dry_run,
            ))

    if args.json:
        print(json.dumps({
            "checked_at": today.isoformat(),
            "dry_run": dry_run,
            "login_failures": [{"course": n, "error": e} for n, e in login_failures],
            "notes": [{"course": n, "note": note} for n, note in notes],
            "targets": [r.as_dict() for r in results],
        }, indent=2))
    else:
        print(f"Harvesting grids for {len(courses)} course(s), {len(results)} teesheet(s) "
              f"{'(dry run)' if dry_run else ''}\n")
        for name, err in login_failures:
            print(f"  [SKIP] {name}: {err}")
        for name, note in notes:
            print(f"  [NOTE] {name}: {note}")
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
