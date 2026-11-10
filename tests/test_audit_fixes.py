"""Regressions for the v1.19 audit fixes that can be checked without a race.

1. upsert_grid must not write when it cannot read the stored row: the
   no-downgrade comparison is the only guard against a thin read replacing a
   release-day grid, and a failed read used to skip that comparison and
   overwrite blind.
2. A forked job worker must not keep the web process's shared HTTP client.
"""
from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["RACE_LOG_WEBHOOK_URL"] = "https://example.invalid/rest/v1/races"

import src.telemetry.grid_store as gs  # noqa: E402
from src.discovery.foreup_client import ForeUpDiscoveryClient  # noqa: E402

FAILURES = 0


def check(name, cond, detail=""):
    global FAILURES
    print(f"  {'ok' if cond else 'FAIL'}: {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES += 1


def test_upsert_skips_when_stored_row_is_unreadable():
    posted = []

    class Resp:
        status_code = 201
        text = ""

    class FakeHttpx:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            raise TimeoutError("read timed out")

        @staticmethod
        def post(url, json=None, headers=None, timeout=None):
            posted.append(json)
            return Resp()

    real, real_delay = builtins.__import__, gs.FETCH_RETRY_DELAY_S
    gs.FETCH_RETRY_DELAY_S = 0
    builtins.__import__ = lambda n, *a, **k: FakeHttpx if n == "httpx" else real(n, *a, **k)
    try:
        wrote = gs.upsert_grid(
            course_id="2431", day_of_week=0,
            grid={"interval_minutes": 10, "first_time": "16:20", "last_time": "16:50",
                  "observed_count": 4, "confidence": "low"},
            template={}, source_date="2026-11-16")
    finally:
        builtins.__import__ = real
        gs.FETCH_RETRY_DELAY_S = real_delay
    check("reports the write as not done", wrote is False)
    check("nothing was posted", not posted, f"posted {posted}")


def test_reset_gives_the_worker_its_own_client():
    parent = ForeUpDiscoveryClient(base_url="https://example.invalid").client
    check("instances share one client before the reset",
          ForeUpDiscoveryClient(base_url="https://example.invalid").client is parent)
    ForeUpDiscoveryClient.reset_shared_client()
    child = ForeUpDiscoveryClient(base_url="https://example.invalid").client
    check("a new client is built after the reset", child is not parent)
    check("the parent's client was left open, not closed", not parent.is_closed)


if __name__ == "__main__":
    for fn in (test_upsert_skips_when_stored_row_is_unreadable,
               test_reset_gives_the_worker_its_own_client):
        print(f"\n{fn.__name__}")
        try:
            fn()
        except Exception:
            FAILURES += 1
            import traceback
            traceback.print_exc()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
