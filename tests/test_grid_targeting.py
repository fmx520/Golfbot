"""Which tee times Future Sight aims at.

A grid mistake does not lose a race polling would have won -- polling runs
alongside the burst -- but it throws away the head start that has been
winning. These pin the ways the targeting could aim at times that do not
exist:

* two intervals meeting (10-minute day, 15-minute twilight) read as one
  "consistent" 5-minute grid, so half the targets are fake times;
* a stale stored grid and tonight's live read disagreeing, with the ranking
  silently picking the stale one (handled in app.py by splitting targets --
  the race-level check lives in test_prestage_concurrency.py);
* aiming before the first time a course's sheet is bookable at all
  (2026-10-17: 48 holds at 07:00-07:51, all refused, while the sheet seen at
  release opened at 08:18 / 09:00) -- including on a weekday with no good
  release read of its own (2026-10-25, Black Sunday: 30 holds at 08:00-08:50).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GOLFBOT_LOG_DIR", "/tmp/golfbot-test-logs")

from src.discovery.grid import analyze_teesheet_grid  # noqa: E402
from src.telemetry.grid_store import release_floor, sibling_release_floor  # noqa: E402
import src.web.app as app  # noqa: E402

FAILURES = 0


def _fail(msg: str) -> None:
    global FAILURES
    FAILURES += 1
    print(f"  FAIL: {msg}")


def _check(label: str, times: list[str], interval, consistent: bool) -> None:
    g = analyze_teesheet_grid(times)
    if (g.interval_minutes if consistent else None, g.consistent) != (interval if consistent else None, consistent):
        return _fail(f"{label}: got interval={g.interval_minutes} consistent={g.consistent}, "
                     f"wanted interval={interval} consistent={consistent}")
    print(f"  ok: {label} -> interval={g.interval_minutes} consistent={g.consistent}")


def test_two_intervals_meeting_are_not_a_5_minute_grid() -> None:
    _check("10-min day into 15-min twilight (GCD 5)",
           ["15:00", "15:10", "15:20", "16:00", "16:15", "16:30"], None, False)
    _check("12 and 18 minute gaps (GCD 6)",
           ["10:00", "10:12", "10:30", "10:42", "11:00"], None, False)


def test_real_sheets_still_read_correctly() -> None:
    _check("9-min sheet, booked gaps", ["11:45", "12:03", "12:30", "13:06", "14:00"], 9, True)
    _check("9-min sheet, only every 2nd/3rd slot open (gaps 18, 27)",
           ["10:03", "10:21", "10:48", "11:06", "11:33"], 9, True)
    _check("10-min sheet", ["16:20", "16:30", "16:40", "16:50"], 10, True)
    _check("genuine 6-min sheet (6 actually observed)",
           ["07:00", "07:06", "07:12", "07:24", "07:30"], 6, True)


def test_grids_disagree() -> None:
    cases = [
        ((9, 3, 9, 3), False, "same grid"),
        ((9, 3, 9, 12), False, "same phase written two ways"),
        ((9, 3, 18, 3), False, "18-min is a sparse view of the 9-min grid"),
        ((9, 3, 18, 12), False, "18-min at :12 still sits on the 9-min @3 grid"),
        ((9, 3, 10, 0), True, "9-min vs 10-min"),
        ((9, 3, 9, 6), True, "same interval, different phase (2437 vs its neighbours)"),
        ((9, 3, 18, 4), True, "18-min that does not sit on the 9-min grid"),
    ]
    for args, want, label in cases:
        got = app._grids_disagree(*args)
        if got != want:
            _fail(f"_grids_disagree{args} ({label}) = {got}, wanted {want}")
        else:
            print(f"  ok: {label} -> disagree={got}")


def test_interleave_targets() -> None:
    got = app._interleave_targets(["08:00", "08:09", "08:18", "08:27"],
                                  ["08:00", "08:10", "08:20", "08:30"], 6)
    want = ["08:00", "08:09", "08:10", "08:18", "08:20", "08:27"]
    if got != want:
        return _fail(f"interleave {got}, wanted {want}")
    print(f"  ok: interleaved, de-duplicated, capped: {got}")


def test_release_floor() -> None:
    """Only a well-observed sheet read at a release sets where aiming starts.

    Rows are the real course_grids rows as of 2026-10-17, as PostgREST sends them.
    """
    cases = [
        ("Red Sat, read at the 10-17 release",
         {"source_date": "2026-10-24", "updated_at": "2026-10-17T23:00:49.33744+00:00",
          "first_time": "08:18", "observed_count": 43}, "08:18"),
        ("Black Sat, read at the 10-17 release",
         {"source_date": "2026-10-24", "updated_at": "2026-10-17T23:01:10.097735+00:00",
          "first_time": "09:00", "observed_count": 32}, "09:00"),
        ("Black Sun: a 4-time read must not floor a morning window at 16:20",
         {"source_date": "2026-09-06", "updated_at": "2026-08-30T23:01:30.469433+00:00",
          "first_time": "16:20", "observed_count": 4}, None),
        ("harvest read 4 days ahead: mornings already booked away",
         {"source_date": "2026-09-05", "updated_at": "2026-09-01T13:57:02.222289+00:00",
          "first_time": "16:42", "observed_count": 30}, None),
        ("7pm EST release is 00:00 UTC the next day",
         {"source_date": "2026-11-14", "updated_at": "2026-11-08T00:00:40+00:00",
          "first_time": "08:30", "observed_count": 30}, "08:30"),
        ("Postgres '+00' offset", {"source_date": "2026-10-24",
          "updated_at": "2026-10-17 23:00:49.33744+00", "first_time": "08:18",
          "observed_count": 43}, "08:18"),
        ("no row", None, None),
    ]
    for label, row, want in cases:
        got = release_floor(row)
        if got != want:
            _fail(f"{label}: floor {got!r}, wanted {want!r}")
        else:
            print(f"  ok: {label} -> {got}")


# Black (2431) as stored when the 10-25 race armed. Sunday's only row was the
# 4-time 08-30 read, which release_floor rightly refuses.
BLACK_AT_10_25 = [
    {"day_of_week": 0, "source_date": "2026-09-07", "updated_at": "2026-08-31 23:01:30.602061+00",
     "first_time": "08:30", "observed_count": 39},
    {"day_of_week": 2, "source_date": "2026-10-28", "updated_at": "2026-10-21 23:00:25.369726+00",
     "first_time": "09:30", "observed_count": 29},
    {"day_of_week": 5, "source_date": "2026-10-24", "updated_at": "2026-10-17 23:01:10.097735+00",
     "first_time": "09:00", "observed_count": 32},
    {"day_of_week": 6, "source_date": "2026-09-06", "updated_at": "2026-08-30 23:01:30.469433+00",
     "first_time": "16:20", "observed_count": 4},
]
# Red (2432) after 10-25: weekday rows are thin harvest reads; Sunday is a
# 12-time read from a snipe-now two minutes after the release.
RED_AT_10_25 = [
    {"day_of_week": 1, "source_date": "2026-09-01", "updated_at": "2026-08-31 22:28:43.978375+00",
     "first_time": "17:09", "observed_count": 6},
    {"day_of_week": 3, "source_date": "2026-09-03", "updated_at": "2026-08-31 15:21:48.646687+00",
     "first_time": "15:30", "observed_count": 11},
    {"day_of_week": 5, "source_date": "2026-10-24", "updated_at": "2026-10-17 23:00:49.33744+00",
     "first_time": "08:18", "observed_count": 43},
    {"day_of_week": 6, "source_date": "2026-11-01", "updated_at": "2026-10-25 23:03:29.700182+00",
     "first_time": "11:54", "observed_count": 12},
]


def test_sibling_release_floor() -> None:
    """A weekday with no good release read borrows from its own day type only."""
    def got(rows, dow):
        b = sibling_release_floor(course_id="x", day_of_week=dow, rows=rows)
        return (b["floor"], b["day_of_week"]) if b else None

    cases = [
        ("Black Sunday borrows Saturday (10-25's race)", BLACK_AT_10_25, 6, ("09:00", 5)),
        ("Black Thursday borrows the EARLIEST weekday (Mon 08:30, not Wed 09:30)",
         BLACK_AT_10_25, 3, ("08:30", 0)),
        ("Black Saturday never borrows its own row, nor a weekday's",
         [r for r in BLACK_AT_10_25 if r["day_of_week"] != 6], 5, None),
        ("Red Sunday borrows Saturday 08:18", RED_AT_10_25, 6, ("08:18", 5)),
        ("Red Tuesday: weekday rows too thin, nothing to borrow", RED_AT_10_25, 1, None),
        ("no rows", [], 6, None),
    ]
    for label, rows, dow, want in cases:
        g = got(rows, dow)
        if g != want:
            _fail(f"{label}: got {g}, wanted {want}")
        else:
            print(f"  ok: {label} -> {g}")


if __name__ == "__main__":
    for fn in (test_two_intervals_meeting_are_not_a_5_minute_grid, test_real_sheets_still_read_correctly,
               test_grids_disagree, test_interleave_targets, test_release_floor,
               test_sibling_release_floor):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
