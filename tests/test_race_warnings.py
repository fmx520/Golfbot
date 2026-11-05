"""A race that was never winnable must say so, in its own summary.

Every failure this bot has had was visible in the data at the time and nobody
saw it. On 2026-11-04, 24 pre-staged holds were refused as "Time not available"
— meaning they fired at times the sheet does not offer — and the race recorded
itself as a plain ABORTED with a neutral-looking "pre-staged 24 (0 accepted)".
Nothing said the race had been lost at arming, an hour before it started.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GOLFBOT_LOG_DIR", "/tmp/golfbot-test-logs")
os.environ.pop("RACE_LOG_WEBHOOK_URL", None)

from src.telemetry.race_log import RaceRecorder  # noqa: E402

FAILURES = 0
OFF_SHEET = ('Client error 400 | Body: {"success":false,"msg":"Time not available."}')
TAKEN = ('Client error 400 | Body: {"success":false,'
         '"message":"Sorry, that tee time is no longer available."}')


def _rec(**kw):
    return RaceRecorder(run_id="t", job_id="j", mode="sniper", course_ids=["2431"],
                        play_date="2026-11-11", release_at="", auto_book=True, **kw)


def check(name, cond, detail=""):
    global FAILURES
    if cond:
        print(f"  ok: {name}")
    else:
        FAILURES += 1
        print(f"FAIL: {name} {detail}")


def _codes(r):
    return [w["code"] for w in r.warnings]


def test_the_2026_11_04_race_now_flags_itself():
    r = _rec()
    for t in ("08:30", "08:40", "08:50", "09:00", "09:10", "09:20"):
        for _ in range(4):                      # four burst passes, as on the night
            r.record_prestage(t, "rejected", 500.0, error=OFF_SHEET)
    r._derive_warnings()
    check("flagged aimed_off_sheet", "aimed_off_sheet" in _codes(r), _codes(r))
    line = r.summary_line()
    check("summary leads with the flag", line.startswith("!! AIMED_OFF_SHEET"), line[:90])
    check("names the times we fired at", "08:30" in line and "09:20" in line, line[:160])


def test_losing_to_a_competitor_is_not_flagged():
    """Being outraced is a race. It is not a targeting failure."""
    r = _rec()
    for t in ("09:30", "09:40"):
        r.record_prestage(t, "rejected", 300.0, error=TAKEN)
    r._derive_warnings()
    check("no warning for a genuine loss", _codes(r) == [], _codes(r))


def test_a_won_hold_is_never_flagged():
    r = _rec()
    r.record_prestage("09:30", "hold_accepted", 400.0)
    r.record_prestage("09:40", "rejected", 300.0, error=OFF_SHEET)
    r._derive_warnings()
    check("an accepted hold clears it", _codes(r) == [], _codes(r))


def test_mixed_rejections_are_not_flagged():
    """Only flag when EVERY rejection says the time does not exist."""
    r = _rec()
    r.record_prestage("09:30", "rejected", 300.0, error=TAKEN)
    r.record_prestage("09:40", "rejected", 300.0, error=OFF_SHEET)
    r._derive_warnings()
    check("mixed causes stay unflagged", _codes(r) == [], _codes(r))


def test_arm_time_warnings_survive_to_the_record():
    r = _rec()
    r.record_warning("stale_grid", "Aiming with a Monday grid last read 58 days ago.")
    r.record_warning("stale_grid", "duplicate should be ignored")
    d = r.as_dict()
    check("stored on the record", len(d["warnings"]) == 1, d["warnings"])
    check("summary carries it", "!! STALE_GRID" in d["summary"], d["summary"][:90])
    check("outcome itself is unchanged", d["outcome"] == "never_saw_slot", d["outcome"])


def test_no_warnings_leaves_the_summary_alone():
    r = _rec()
    r.record_poll(300.0, 0)
    line = r.summary_line()
    check("clean races read as before", not line.startswith("!!"), line[:70])


if __name__ == "__main__":
    for fn in (test_the_2026_11_04_race_now_flags_itself,
               test_losing_to_a_competitor_is_not_flagged,
               test_a_won_hold_is_never_flagged,
               test_mixed_rejections_are_not_flagged,
               test_arm_time_warnings_survive_to_the_record,
               test_no_warnings_leaves_the_summary_alone):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
