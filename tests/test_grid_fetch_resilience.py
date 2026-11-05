"""A failed grid read must never be mistaken for "we have never seen this day".

2026-11-04, Bethpage Black, play date Wednesday 2026-11-11. One 5-second
ReadTimeout in fetch_grid made the stored Wednesday grid invisible. The caller
read None as "never seen", inferred the rhythm from Mon/Sat/Sun, and borrowed a
release floor from a Monday read taken two months earlier (08:30). The real
Wednesday sheet started at 09:30, so all 24 pre-staged holds came back
"Time not available." while the row sat in the table the whole time saying
09:30-16:00, 29 slots.
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
WEDNESDAY_ROW = {
    "course_id": "2431", "day_of_week": 2, "interval_minutes": 10,
    "anchor_phase": 0, "first_time": "09:30", "last_time": "16:00",
    "observed_count": 29, "confidence": "high", "source_date": "2026-10-28",
}


def _harness(behaviours):
    """behaviours: list of 'timeout' | 'row' | 'empty' | '500', one per attempt."""
    calls = {"n": 0}

    class Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self._p = payload
            self.text = "boom"

        def json(self):
            return self._p

    class FakeHttpx:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            i = calls["n"]
            calls["n"] += 1
            b = behaviours[i] if i < len(behaviours) else behaviours[-1]
            if b == "timeout":
                raise TimeoutError("The read operation timed out")
            if b == "500":
                return Resp(500, None)
            if b == "row":
                return Resp(200, [WEDNESDAY_ROW])
            return Resp(200, [])

    real = builtins.__import__
    builtins.__import__ = lambda n, *a, **k: FakeHttpx if n == "httpx" else real(n, *a, **k)
    gs.FETCH_RETRY_DELAY_S = 0.0
    try:
        return gs.fetch_grid_result(course_id="2431", day_of_week=2), calls["n"]
    finally:
        builtins.__import__ = real


def check(name, cond, detail=""):
    global FAILURES
    if cond:
        print(f"  ok: {name}")
    else:
        FAILURES += 1
        print(f"FAIL: {name} {detail}")


def test_the_2026_11_04_timeout_now_recovers():
    (row, ok), n = _harness(["timeout", "row"])
    check("retried after the timeout", n == 2, f"attempts={n}")
    check("found the Wednesday grid", row and row["first_time"] == "09:30", row)
    check("reported a good read", ok is True)


def test_absent_row_is_not_an_error():
    (row, ok), n = _harness(["empty"])
    check("no row returned", row is None)
    check("but the read SUCCEEDED — inference is legitimate here", ok is True)
    check("did not waste retries on a successful read", n == 1, f"attempts={n}")


def test_total_failure_reports_not_ok():
    (row, ok), n = _harness(["timeout", "timeout", "timeout"])
    check("exhausted the attempts", n == gs.FETCH_ATTEMPTS, f"attempts={n}")
    check("row is None", row is None)
    check("ok is FALSE — caller must not infer", ok is False,
          "a read failure reported as success is the 11-04 bug")


def test_http_error_also_retries():
    (row, ok), n = _harness(["500", "row"])
    check("retried past a 500", n == 2 and ok is True and row is not None, (row, ok, n))


def test_legacy_fetch_grid_still_returns_a_row():
    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return [WEDNESDAY_ROW]

    real = builtins.__import__

    class FakeHttpx:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            return Resp()

    builtins.__import__ = lambda n, *a, **k: FakeHttpx if n == "httpx" else real(n, *a, **k)
    try:
        row = gs.fetch_grid(course_id="2431", day_of_week=2)
    finally:
        builtins.__import__ = real
    check("upsert_grid's caller still works", row and row["first_time"] == "09:30", row)


if __name__ == "__main__":
    for fn in (test_the_2026_11_04_timeout_now_recovers, test_absent_row_is_not_an_error,
               test_total_failure_reports_not_ok, test_http_error_also_retries,
               test_legacy_fetch_grid_still_returns_a_row):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
