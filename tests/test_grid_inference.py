"""Infer an unseen weekday's grid from the same course's other days.

A tee sheet's interval and phase belong to the course, not the day — across
every course read on more than one day, all days agree. Without this, learning
Saturday requires waiting for an actual Saturday; with it Bethpage Black goes
from 2 aimable days to 7.
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


def _row(dow, ivl=9, ph=3, seen=30, first="08:00", last="17:00", fee=55):
    return {"course_id": "2431", "day_of_week": dow, "interval_minutes": ivl,
            "anchor_phase": ph, "first_time": first, "last_time": last,
            "observed_count": seen, "confidence": "high",
            "template": {"green_fee": fee, "teesheet_side_id": "1014"},
            "source_date": f"2026-09-0{dow+1}"}


def _with_rows(rows, fn):
    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return rows

    real = builtins.__import__

    class FakeHttpx:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            return Resp()

    builtins.__import__ = lambda n, *a, **k: FakeHttpx if n == "httpx" else real(n, *a, **k)
    try:
        return fn()
    finally:
        builtins.__import__ = real


def check(name, cond, detail=""):
    global FAILURES
    if cond:
        print(f"  ok: {name}")
    else:
        FAILURES += 1
        print(f"FAIL: {name} {detail}")


def test_infers_from_agreeing_siblings():
    rows = [_row(0), _row(1), _row(2)]
    got = _with_rows(rows, lambda: gs.infer_grid(course_id="2431", day_of_week=5))
    check("inferred a grid", got is not None)
    check("interval carried", got and got["interval_minutes"] == 9, got)
    check("phase carried", got and got["anchor_phase"] == 3, got)
    check("observations summed", got and got["observed_count"] == 90, got)
    check("window deliberately blank", got and got["first_time"] == "" and got["last_time"] == "", got)
    check("records provenance", got and got["inferred_from"] == [0, 1, 2], got)


def test_refuses_when_siblings_disagree():
    """A course really running two intervals must not be papered over."""
    rows = [_row(0, ivl=9), _row(1, ivl=10)]
    got = _with_rows(rows, lambda: gs.infer_grid(course_id="2431", day_of_week=5))
    check("refuses on disagreement", got is None, got)


def test_refuses_on_thin_evidence():
    rows = [_row(0, seen=3), _row(1, seen=4)]
    got = _with_rows(rows, lambda: gs.infer_grid(course_id="2431", day_of_week=5))
    check("refuses when barely observed", got is None, got)


def test_refuses_with_one_sibling():
    got = _with_rows([_row(0, seen=48)], lambda: gs.infer_grid(course_id="2431", day_of_week=5))
    check("one day is not agreement", got is None, got)


def test_template_prefers_same_day_type():
    """Green fees differ weekend vs weekday, so take the template from a peer."""
    rows = [_row(0, seen=40, fee=55), _row(1, seen=35, fee=55), _row(6, seen=20, fee=80)]
    got = _with_rows(rows, lambda: gs.infer_grid(course_id="2431", day_of_week=5))
    check("weekend target took weekend template",
          got and got["inferred_template_dow"] == 6 and got["template"]["green_fee"] == 80, got)
    got2 = _with_rows(rows, lambda: gs.infer_grid(course_id="2431", day_of_week=2))
    check("weekday target took strongest weekday template",
          got2 and got2["inferred_template_dow"] == 0 and got2["template"]["green_fee"] == 55, got2)


def test_excludes_the_target_day_itself():
    rows = [_row(0), _row(5, ivl=99, ph=1, seen=1)]
    got = _with_rows(rows, lambda: gs.infer_grid(course_id="2431", day_of_week=5))
    check("target day excluded from its own inference", got is None or got["interval_minutes"] == 9, got)


if __name__ == "__main__":
    for fn in (test_infers_from_agreeing_siblings, test_refuses_when_siblings_disagree,
               test_refuses_on_thin_evidence, test_refuses_with_one_sibling,
               test_template_prefers_same_day_type, test_excludes_the_target_day_itself):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
