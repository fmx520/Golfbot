"""Which tee times Future Sight aims at.

A grid mistake does not lose a race polling would have won -- polling runs
alongside the burst -- but it throws away the head start that has been
winning. These pin the two ways the targeting could aim at times that do not
exist:

* two intervals meeting (10-minute day, 15-minute twilight) read as one
  "consistent" 5-minute grid, so half the targets are fake times;
* a stale stored grid and tonight's live read disagreeing, with the ranking
  silently picking the stale one (handled in app.py by splitting targets --
  the race-level check lives in test_prestage_concurrency.py).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GOLFBOT_LOG_DIR", "/tmp/golfbot-test-logs")

from src.discovery.grid import analyze_teesheet_grid  # noqa: E402
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


if __name__ == "__main__":
    for fn in (test_two_intervals_meeting_are_not_a_5_minute_grid, test_real_sheets_still_read_correctly,
               test_grids_disagree, test_interleave_targets):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
