"""A thin teesheet read must not clobber a better stored grid.

The booking API returns ONLY open slots, so a nearly-booked day yields a
self-consistent grid describing one corner of the sheet. upsert_grid writes
with merge-duplicates (a blind overwrite), so without this guard the weekly
harvester can replace a grid learned from a freshly released sheet with a
twilight sample -- which is how pre-staged holds ended up firing at 07:06 and
07:15 into a 10-minute morning sheet.
"""
from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["RACE_LOG_WEBHOOK_URL"] = "https://example.invalid/rest/v1/races"

import src.telemetry.grid_store as gs  # noqa: E402

FAILURES = 0
# A good, FRESH stored grid. Its dates are relative to today on purpose: with
# the fixed 2026-08-31 it once had, the row aged past GRID_STALE_DAYS on
# 2026-10-15 and the code -- correctly -- let thin reads replace it, so these
# tests started failing on their own with nothing in the code changed.
from datetime import date as _date, timedelta as _timedelta  # noqa: E402
_GOOD_READ = _date.today() - _timedelta(days=3)  # sheet date lands in the future, not today
GOOD = {"course_id": "2431", "day_of_week": 0, "interval_minutes": 10,
        "anchor_phase": 0, "first_time": "08:30", "last_time": "17:00",
        "observed_count": 39, "confidence": "high",
        "source_date": (_GOOD_READ + _timedelta(days=7)).isoformat(),
        "updated_at": f"{_GOOD_READ.isoformat()} 23:01:30+00"}


def _harness(existing, new_grid, source_date):
    """Return (wrote?, payload) for an upsert against `existing`."""
    posted = []

    class Resp:
        def __init__(self, code, payload=None):
            self.status_code = code
            self._p = payload if payload is not None else []
            self.text = ""

        def json(self):
            return self._p

    class FakeHttpx:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            return Resp(200, [existing] if existing else [])

        @staticmethod
        def post(url, json=None, headers=None, timeout=None):
            posted.append(json)
            return Resp(201)

    real = builtins.__import__
    builtins.__import__ = lambda n, *a, **k: FakeHttpx if n == "httpx" else real(n, *a, **k)
    try:
        gs.upsert_grid(course_id="2431", day_of_week=0, grid=new_grid,
                       template={"green_fee": 80}, source_date=source_date)
    finally:
        builtins.__import__ = real
    return (len(posted) > 0), (posted[0] if posted else None)


def check(name, cond, detail=""):
    global FAILURES
    if cond:
        print(f"  ok: {name}")
    else:
        FAILURES += 1
        print(f"FAIL: {name} {detail}")


def test_thin_twilight_sample_does_not_clobber():
    thin = {"interval_minutes": 9, "first_time": "16:20", "last_time": "17:00",
            "observed_count": 4, "confidence": "low"}
    wrote, _ = _harness(GOOD, thin, "2026-09-14")
    check("thin sample rejected", wrote is False,
          "a 40min/4-slot read overwrote a 510min/39-slot grid")


def test_better_sample_replaces():
    better = {"interval_minutes": 10, "first_time": "07:00", "last_time": "18:00",
              "observed_count": 55, "confidence": "high"}
    wrote, row = _harness(GOOD, better, "2026-09-14")
    check("wider sample accepted", wrote is True)
    check("stored the new values", row and row["observed_count"] == 55, row)


def test_same_date_reread_refreshes():
    """Re-reading the same date is a refresh, not a downgrade."""
    thinner = {"interval_minutes": 10, "first_time": "09:00", "last_time": "16:00",
               "observed_count": 20, "confidence": "medium"}
    wrote, _ = _harness(GOOD, thinner, GOOD["source_date"])
    check("same-date re-read still writes", wrote is True)


def test_stale_row_gives_way():
    stale = dict(GOOD, updated_at="2026-01-01 00:00:00+00", source_date="2026-01-01")
    thin = {"interval_minutes": 9, "first_time": "16:20", "last_time": "17:00",
            "observed_count": 4, "confidence": "low"}
    wrote, _ = _harness(stale, thin, "2026-09-14")
    check("stale grid is replaced rather than trusted forever", wrote is True)


def test_first_ever_write_succeeds():
    grid = {"interval_minutes": 9, "first_time": "07:42", "last_time": "17:45",
            "observed_count": 48, "confidence": "high"}
    wrote, row = _harness(None, grid, "2026-09-06")
    check("writes when nothing stored", wrote is True)
    check("anchor phase computed", row and row["anchor_phase"] == (7 * 60 + 42) % 9, row)


def test_todays_shrinking_sheet_does_not_thin_a_grid():
    """Today's sheet only shrinks -- a later read is narrower, not truer.

    Observed 2026-09-01: course 2432 Tuesday went 16:42->17:09 and 7->6 slots
    across a single morning, purely from tee times passing.
    """
    from datetime import date
    today = date.today().isoformat()
    morning = dict(GOOD, first_time="16:42", last_time="18:21",
                   observed_count=7, source_date=today)
    later = {"interval_minutes": 9, "first_time": "17:09", "last_time": "18:21",
             "observed_count": 6, "confidence": "medium"}
    wrote, _ = _harness(morning, later, today)
    check("later same-day read rejected when thinner", wrote is False,
          "today's shrinking sheet overwrote the earlier, wider read")


def test_todays_wider_read_still_writes():
    """Only the shrink is blocked -- a genuinely better read of today wins."""
    from datetime import date
    today = date.today().isoformat()
    early = dict(GOOD, first_time="17:00", last_time="18:00",
                 observed_count=5, source_date=today)
    wider = {"interval_minutes": 9, "first_time": "16:00", "last_time": "18:21",
             "observed_count": 14, "confidence": "high"}
    wrote, _ = _harness(early, wider, today)
    check("wider same-day read accepted", wrote is True)


def test_future_date_reread_still_refreshes():
    """A future date's sheet does not shrink with the clock, so refresh it."""
    future = dict(GOOD, source_date="2026-12-25", first_time="08:00",
                  last_time="17:00", observed_count=30)
    thinner = {"interval_minutes": 10, "first_time": "09:00", "last_time": "16:00",
               "observed_count": 25, "confidence": "medium"}
    wrote, _ = _harness(future, thinner, "2026-12-25")
    check("future same-date re-read refreshes", wrote is True)


if __name__ == "__main__":
    for fn in (test_thin_twilight_sample_does_not_clobber, test_better_sample_replaces,
               test_same_date_reread_refreshes, test_stale_row_gives_way,
               test_first_ever_write_succeeds,
               test_todays_shrinking_sheet_does_not_thin_a_grid,
               test_todays_wider_read_still_writes,
               test_future_date_reread_still_refreshes):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
