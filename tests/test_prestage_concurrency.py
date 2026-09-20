"""Race-critical behaviour of the Future Sight burst.

These exist because the burst is the only part of the bot that runs off the
main thread, and it runs at the exact moment a race is decided. The properties
below were each bought with a lost race:

* the burst must not delay polling (2026-08-31: it did, we fired at T+801ms)
* every target must go out at T+0, not queued behind the one before it
  (2026-08-30: T+0.000, T+0.568, T+2.443, T+4.204)
* holds we win but do not book must go back to the sheet, not be squatted
* exactly one booking may ever be completed
"""
from __future__ import annotations

import os
import sys
import threading
import time
from multiprocessing import Queue
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("GOLFBOT_LOG_DIR", "/tmp/golfbot-test-logs")
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_SERVICE_KEY", None)

import src.web.app as app  # noqa: E402


class FakeDiscovery:
    def __init__(self, *a, **kw):
        self.client = None
        self.slots_after = kw.get("slots_after", 999.0)
        self._t0 = time.perf_counter()

    def bootstrap_session(self, **kw):
        return None

    def refresh_session(self, **kw):
        return None

    def get_customer_id(self, **kw):
        return "cust-1"

    def extract_recaptcha_sitekey(self, **kw):
        return ""

    def resolve_booking_class(self, teesheet_id, requested=""):
        return requested or "50294"

    def lookup_availability(self, **kw):
        # Reference sheet for grid learning: a thin evening sample, which is
        # exactly the shape that used to disarm Future Sight.
        return [
            {"time": f"2026-09-07 {h:02d}:{m:02d}", "raw": {"booking_class_id": "50294",
                                                            "teesheet_side_id": "1014",
                                                            "green_fee": 80}}
            for h, m in [(16, 20), (16, 30), (16, 40), (16, 50)]
        ]

    def lookup_availability_fast(self, **kw):
        POLLS.append(time.perf_counter())
        # SLOW_POLL_S lets a test make the burst land mid-poll, which is the
        # window the 2026-09-19 self-collision happened in.
        time.sleep(FakeDiscovery.SLOW_POLL_S)
        return list(FakeDiscovery.POLL_SLOTS)

    SLOW_POLL_S = 0.05
    POLL_SLOTS: list = []


POLLS: list[float] = []
POLL_BOOKS: list[str] = []
FIRES: list[tuple[str, float]] = []
RELEASED: list[str] = []
COMPLETED: list[str] = []


class FakeBooking:
    """Holds are accepted for whichever times ACCEPT names."""
    ACCEPT: set[str] = set()
    HOLD_LATENCY = 0.20

    def __init__(self, *a, **kw):
        pass

    def probe_cold_hold(self, *, target_time, **kw):
        hhmm = target_time.split(" ")[1]
        FIRES.append((hhmm, time.perf_counter()))
        time.sleep(self.HOLD_LATENCY)
        if hhmm in self.ACCEPT:
            return {"accepted": True, "latency_ms": self.HOLD_LATENCY * 1000,
                    "reservation_id": f"TTID_{hhmm.replace(':','')}"}
        return {"accepted": False, "latency_ms": self.HOLD_LATENCY * 1000,
                "error": 'Client error 400 | Body: {"success":false,"msg":"Time not available."}'}

    def release_pending_reservation(self, *, pending_reservation_id, **kw):
        RELEASED.append(pending_reservation_id)
        return True

    def complete_prestaged_hold(self, *, target_time, **kw):
        COMPLETED.append(target_time)
        return {"success": True, "reservation_id": "R1", "TTID": "T1"}

    def book_slot(self, *, slot=None, **kw):
        POLL_BOOKS.append((slot or {}).get("time", "?"))
        raise RuntimeError('{"success":false,"message":"Sorry, that tee time is '
                           'no longer available."}')

    def refresh_pending_reservation(self, **kw):
        return None


def _align_to_release(lead_s: float = 4.0) -> str:
    """Return an HH:MM release that lands `lead_s` from now.

    The worker sleeps until the release on the monotonic clock, so the test
    parks just before a minute boundary rather than faking time -- this
    exercises the real countdown path.
    """
    from datetime import datetime, timedelta
    nxt = (int(time.time()) // 60 + 1) * 60
    while nxt - time.time() > lead_s:
        time.sleep(0.05)
        if nxt - time.time() > 65:
            break
    # The worker reads HH:MM as US/Eastern; this container runs UTC.
    return (datetime.fromtimestamp(nxt, app.EAST_COAST_TZ)).strftime("%H:%M")


def _run(accept: set[str], timeout_s: float = 30.0, preserve_fakes: bool = False) -> dict:
    POLLS.clear(); FIRES.clear(); RELEASED.clear(); COMPLETED.clear(); POLL_BOOKS.clear()
    if not preserve_fakes:
        FakeDiscovery.SLOW_POLL_S = 0.05
        FakeDiscovery.POLL_SLOTS = []
    FakeBooking.ACCEPT = set(accept)

    app.ForeUpDiscoveryClient = FakeDiscovery
    app.ForeUpBookingClient = FakeBooking

    release = _align_to_release()
    payload = {
        "play_date": "2026-09-07",
        "players": 1, "holes": 18,
        "time_start": "08:30", "time_end": "09:45",
        "release_time": release,
        "auto_book": True, "dry_run": False,
        "future_sight": True,
        "prestage_times": ["08:30", "08:50", "09:10", "09:40"],
        "course_ids": ["2431"], "schedule_id": "2431", "course_id": "2431",
        "username": "u", "password": "p",
        "timeout_seconds": 6,
    }
    q: Queue = Queue()
    cq: Queue = Queue()
    t = threading.Thread(target=app._sniper_worker_main,
                         args=("jobtest", payload, q, cq), daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    events = []
    while not q.empty():
        try:
            events.append(q.get_nowait())
        except Exception:
            break
    if os.getenv("DUMP_EVENTS"):
        for e in events:
            if e.get("type") == "event":
                print(f"    [{e.get('phase')}/{e.get('stage')}] {str(e.get('message'))[:200]}")
            else:
                print(f"    {e}")
    return {"events": events, "alive": t.is_alive()}


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    globals()["FAILURES"] += 1


FAILURES = 0


def test_targets_fire_in_parallel() -> None:
    """All four targets must leave at T+0, not queued behind each other.

    Before this, target 4 went out 4.2s after the release -- into a race that
    had been over for four seconds.
    """
    _run(accept=set())
    first_pass = FIRES[:4]
    if len(first_pass) < 4:
        return _fail(f"expected 4 fires in the first pass, saw {len(first_pass)}")
    spread = max(t for _, t in first_pass) - min(t for _, t in first_pass)
    if spread > 0.10:
        return _fail(f"targets fired {spread*1000:.0f}ms apart — still sequential")
    print(f"  ok: 4 targets fired within {spread*1000:.1f}ms of each other")


def test_polling_runs_during_the_burst() -> None:
    """The burst must not delay polling. This is the 2026-08-31 loss."""
    _run(accept=set())
    if not FIRES or not POLLS:
        return _fail("expected both fires and polls")
    burst_start = min(t for _, t in FIRES)
    burst_end = max(t for _, t in FIRES)
    during = [p for p in POLLS if burst_start <= p <= burst_end]
    if not during:
        return _fail("no poll happened while the burst was running — still sequential")
    print(f"  ok: {len(during)} polls ran during the burst window")


def test_extra_holds_are_released() -> None:
    """Winning several holds must not leave us squatting on tee times."""
    _run(accept={"08:30", "08:50", "09:10", "09:40"})
    if len(COMPLETED) != 1:
        return _fail(f"expected exactly 1 completed booking, got {len(COMPLETED)}: {COMPLETED}")
    if len(RELEASED) < 3:
        return _fail(f"expected >=3 holds released, got {len(RELEASED)}: {RELEASED}")
    print(f"  ok: booked {COMPLETED[0]}, released {len(RELEASED)} extra holds")


def test_single_win_books_once() -> None:
    _run(accept={"08:50"})
    if len(COMPLETED) != 1 or not COMPLETED[0].endswith("08:50"):
        return _fail(f"expected one booking at 08:50, got {COMPLETED}")
    if RELEASED:
        return _fail(f"nothing should have been released, got {RELEASED}")
    print(f"  ok: single hold booked once, nothing released")


def test_win_landing_mid_poll_beats_the_poll_path():
    """A hold won DURING a poll must be taken before the poll path submits.

    2026-09-19: the burst won 09:39 at T+1.0s, the poll cycle ran until T+3.9s,
    and the poll path then spent its submit on 09:03 -- a slot our own burst
    had held and released seconds earlier -- and lost it. The win was not
    touched until T+4.1s. Checking only at the top of the loop is not enough.
    """
    FakeDiscovery.SLOW_POLL_S = 1.2          # burst lands mid-poll
    FakeDiscovery.POLL_SLOTS = [             # something for the poll to book
        {"time": "09:10", "available_spots": 4, "green_fee": 80,
         "raw": {"schedule_id": "2436", "course_id": "2436"}},
    ]
    FakeBooking.HOLD_LATENCY = 0.2
    _run(accept={"09:40"}, preserve_fakes=True)
    if not COMPLETED or not COMPLETED[0].endswith("09:40"):
        return _fail(f"prestaged hold was not booked: {COMPLETED}")
    if POLL_BOOKS:
        return _fail(f"poll path submitted against {POLL_BOOKS} while a won hold was pending")
    print("  ok: won hold taken before the poll path could submit")


if __name__ == "__main__":
    os.environ["SNIPER_RACE_TIMEOUT_SECONDS"] = "4"
    os.environ["SNIPER_PRESTAGE_BURST_SECONDS"] = "2"
    for fn in (test_targets_fire_in_parallel, test_polling_runs_during_the_burst,
               test_extra_holds_are_released, test_single_win_books_once,
               test_win_landing_mid_poll_beats_the_poll_path):
        print(f"\n{fn.__name__}")
        try:
            fn()
        except Exception as exc:
            FAILURES += 1
            import traceback; traceback.print_exc()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
