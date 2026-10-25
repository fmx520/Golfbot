"""Race-critical behaviour of the Future Sight burst.

These exist because the burst is the only part of the bot that runs off the
main thread, and it runs at the exact moment a race is decided. The properties
below were each bought with a lost race:

* the burst must not delay polling (2026-08-31: it did, we fired at T+801ms)
* every target must go out at T+0, not queued behind the one before it
  (2026-08-30: T+0.000, T+0.568, T+2.443, T+4.204)
* holds we win but do not book must go back to the sheet, not be squatted
* exactly one booking may ever be completed
* a won hold on a fee-charging course must end in a payment link, not be
  given away (2026-10-10: three Bethpage holds won, all thrown back)
* when a stored grid and tonight's sheet disagree, both get shots -- a stale
  grid must not quietly aim every hold at times that no longer exist
* holds aim no earlier than the first time a release has shown bookable
  (2026-10-17: 48 holds at 07:00-07:51 refused; the sheet opened 08:18/09:00)
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

    # Reference sheet for grid learning: a thin evening sample, which is
    # exactly the shape that used to disarm Future Sight.
    REF_TIMES = [(16, 20), (16, 30), (16, 40), (16, 50)]

    def lookup_availability(self, **kw):
        return [
            {"time": f"2026-09-07 {h:02d}:{m:02d}", "raw": {"booking_class_id": "50294",
                                                            "teesheet_side_id": "1014",
                                                            "green_fee": 80}}
            for h, m in FakeDiscovery.REF_TIMES
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
REFRESHED: list[str] = []


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

    # What completing a pre-staged hold returns. None = a normal booking;
    # a dict = that response (e.g. awaiting_payment on a fee-charging course).
    COMPLETE_RESPONSE: dict | None = None
    # Refresh results in order; once exhausted, refreshes keep succeeding.
    REFRESH_RESULTS: list = []

    def complete_prestaged_hold(self, *, target_time, pending_reservation_id="", **kw):
        COMPLETED.append(target_time)
        if self.COMPLETE_RESPONSE is not None:
            return {**self.COMPLETE_RESPONSE, "pending_reservation_id": pending_reservation_id}
        return {"success": True, "reservation_id": "R1", "TTID": "T1"}

    def book_slot(self, *, slot=None, **kw):
        POLL_BOOKS.append((slot or {}).get("time", "?"))
        raise RuntimeError('{"success":false,"message":"Sorry, that tee time is '
                           'no longer available."}')

    def refresh_pending_reservation(self, *, pending_reservation_id="", **kw):
        REFRESHED.append(pending_reservation_id)
        return FakeBooking.REFRESH_RESULTS.pop(0) if FakeBooking.REFRESH_RESULTS else None


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


def _run(accept: set[str], timeout_s: float = 30.0, preserve_fakes: bool = False,
         payload_overrides: dict | None = None) -> dict:
    POLLS.clear(); FIRES.clear(); RELEASED.clear(); COMPLETED.clear(); POLL_BOOKS.clear()
    REFRESHED.clear()
    if not preserve_fakes:
        FakeDiscovery.SLOW_POLL_S = 0.05
        FakeDiscovery.POLL_SLOTS = []
        FakeBooking.COMPLETE_RESPONSE = None
        FakeBooking.REFRESH_RESULTS = []
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
    for k, v in (payload_overrides or {}).items():
        if v is None:
            payload.pop(k, None)
        else:
            payload[k] = v
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


def test_fee_course_prestaged_win_ends_in_payment_link():
    """A won pre-staged hold on a fee-charging course must hand back the link.

    2026-10-10, Bethpage 2432: the burst won 08:36, 08:45 and 08:27 at T+2s.
    The pre-staged completion had no booking-fee recovery (it only ever lived
    in the poll path's book_slot), so "A booking fee is required" ended it, the
    hold was abandoned, and the race was recorded as lost. The poll path would
    have produced a $5 payment link -- as it did three times on 08-31.

    The hold must then be kept alive while the user pays. That keepalive used
    to be a daemon thread started just before the worker returned, so it died
    with the process and never sent a refresh.
    """
    FakeBooking.COMPLETE_RESPONSE = {
        "awaiting_payment": True,
        "payment_url": "https://hostedpayments.example/pay?TransactionSetupID=abc",
        "booking_fee_total": 5, "players": 1, "slot_time": "08:50",
    }
    # Two refreshes land, then the hold stops refreshing (paid or gone):
    # the keepalive must notice and end on its own.
    FakeBooking.REFRESH_RESULTS = [None, None, "400 gone", "400 gone", "400 gone"]
    app.PAYMENT_HOLD_REFRESH_S = 0.2
    try:
        out = _run(accept={"08:50"}, preserve_fakes=True)
    finally:
        app.PAYMENT_HOLD_REFRESH_S = 8.0
        FakeBooking.COMPLETE_RESPONSE = None
        FakeBooking.REFRESH_RESULTS = []
    events = out["events"]
    if out["alive"]:
        return _fail("worker never finished -- keepalive did not stop on failed refreshes")
    if len(COMPLETED) != 1 or not COMPLETED[0].endswith("08:50"):
        return _fail(f"expected the 08:50 hold to be completed once, got {COMPLETED}")
    pay = [e for e in events if e.get("type") == "event" and e.get("stage") == "awaiting_payment"]
    if not pay or not pay[0].get("payment_url"):
        return _fail("no awaiting_payment event with a payment_url was emitted")
    statuses = [e.get("status") for e in events if e.get("type") == "status"]
    if "awaiting_payment" not in statuses or "succeeded" in statuses or "failed" in statuses:
        return _fail(f"expected to end awaiting_payment, statuses were {statuses}")
    if "TTID_0850" in RELEASED:
        return _fail("the held tee time was released instead of kept for payment")
    if POLL_BOOKS:
        return _fail(f"poll path kept racing after the hold was won: {POLL_BOOKS}")
    summary = [e for e in events if e.get("type") == "event" and e.get("stage") == "race_summary"]
    outcome = (summary[0].get("race") or {}).get("outcome") if summary else None
    if outcome != "awaiting_payment":
        return _fail(f"race recorded as {outcome!r}, expected 'awaiting_payment'")
    if REFRESHED.count("TTID_0850") < 3:
        return _fail(f"hold was not kept alive after the race: refreshes={REFRESHED}")
    print(f"  ok: payment link handed back, hold kept ({len(REFRESHED)} refreshes), "
          f"race recorded as {outcome}")


def test_disagreeing_grids_split_the_targets():
    """A stale stored grid must not silently take every shot.

    The ranking prefers a grid that covers the window and has been seen more,
    and never looks at age -- so a six-week-old 9-minute grid beats tonight's
    live 10-minute read every time. If the course changed its sheet, all six
    holds would go to times that no longer exist. Both grids must get shots.
    """
    from src.telemetry import grid_store
    stored = {"interval_minutes": 9, "anchor_phase": 3, "first_time": "07:00",
              "last_time": "17:00", "observed_count": 40, "source_date": "2026-09-01",
              "template": {"booking_class_id": "50294", "teesheet_side_id": "1014",
                           "green_fee": 80}}
    real_fetch = grid_store.fetch_grid
    grid_store.fetch_grid = lambda **kw: dict(stored)
    FakeDiscovery.REF_TIMES = [(16, 10), (16, 20), (16, 30), (16, 40), (16, 50), (17, 0)]
    try:
        out = _run(accept=set(), payload_overrides={
            "prestage_times": None, "time_start": "08:00", "time_end": "09:00"})
    finally:
        grid_store.fetch_grid = real_fetch
        FakeDiscovery.REF_TIMES = [(16, 20), (16, 30), (16, 40), (16, 50)]
    fired = sorted({t for t, _ in FIRES})
    nine_only = [t for t in fired if int(t[3:]) % 10 != 0]       # 08:09, 08:18, 08:27 ...
    ten_only = [t for t in fired if int(t[:2]) * 60 + int(t[3:]) not in
                {480 + 9 * k for k in range(8)}]                   # 08:10, 08:20 ...
    if not fired:
        return _fail("no pre-staged holds fired at all")
    if not nine_only or not ten_only:
        return _fail(f"targets {fired} come from one grid only -- the other got no shots")
    split = [e for e in out["events"] if e.get("type") == "event"
             and e.get("stage") == "prestage_grid" and "disagree" in str(e.get("message"))]
    if not split:
        return _fail("no prestage_grid event explaining the split")
    print(f"  ok: fired {fired} -- stored 9-min grid got {nine_only}, live 10-min grid got {ten_only}")


def test_holds_start_at_the_release_floor():
    """Holds aim from where bookable times start, not from the window start.

    2026-10-17: the window opened at 07:00, so all six targets went to 07:00-07:51
    on Black and 07:06-07:51 on Red -- 48 holds, every one refused -- while the
    fullest sheet seen at that release started at 09:00 and 08:18.
    """
    from src.telemetry import grid_store
    stored = {"interval_minutes": 10, "anchor_phase": 0, "first_time": "08:20",
              "last_time": "16:00", "observed_count": 30, "confidence": "high",
              # Read at a release: the play date is a week after the read.
              "source_date": "2026-09-07", "updated_at": "2026-08-31T23:00:30+00:00",
              "template": {"booking_class_id": "50294", "teesheet_side_id": "1014",
                           "green_fee": 80}}
    real_fetch = grid_store.fetch_grid
    grid_store.fetch_grid = lambda **kw: dict(stored)
    try:
        out = _run(accept=set(), payload_overrides={
            "prestage_times": None, "time_start": "08:00", "time_end": "09:00"})
    finally:
        grid_store.fetch_grid = real_fetch
    fired = sorted({t for t, _ in FIRES})
    if not fired:
        return _fail("no pre-staged holds fired at all")
    early = [t for t in fired if t < "08:20"]
    if early:
        return _fail(f"fired before the release floor 08:20: {early} (all fired: {fired})")
    told = [e for e in out["events"] if e.get("type") == "event"
            and e.get("stage") == "prestage_grid" and "earliest open time" in str(e.get("message"))]
    if not told:
        return _fail("no prestage_grid decision explaining the floor")
    print(f"  ok: window 08:00-09:00, floor 08:20 -> fired {fired}")


def test_weekday_without_a_floor_borrows_one():
    """10-25, Black Sunday: no trustworthy Sunday release read, so no floor --
    30 holds at 08:00-08:50, all refused, while Black's Saturday release opened
    at 09:00 and polling's first sight in the window was 09:30. The same
    course's Saturday floor must apply."""
    from src.telemetry import grid_store
    sunday_4_time_read = {"interval_minutes": 10, "anchor_phase": 0, "first_time": "16:20",
                          "last_time": "17:00", "observed_count": 4, "confidence": "low",
                          "source_date": "2026-09-06", "updated_at": "2026-08-30 23:01:30.469433+00",
                          "template": {"booking_class_id": "50294", "teesheet_side_id": "1014",
                                       "green_fee": 80}}
    black_rows = [
        {"day_of_week": 0, "interval_minutes": 10, "anchor_phase": 0, "source_date": "2026-09-07",
         "updated_at": "2026-08-31 23:01:30.602061+00", "first_time": "08:30", "observed_count": 39},
        {"day_of_week": 5, "interval_minutes": 10, "anchor_phase": 0, "source_date": "2026-10-24",
         "updated_at": "2026-10-17 23:01:10.097735+00", "first_time": "09:00", "observed_count": 32},
        dict(sunday_4_time_read, day_of_week=6),
    ]
    real_fetch, real_course = grid_store.fetch_grid, grid_store.fetch_course_grids
    grid_store.fetch_grid = lambda **kw: dict(sunday_4_time_read)
    grid_store.fetch_course_grids = lambda **kw: [dict(r) for r in black_rows]
    try:
        # 2026-11-01 is a Sunday, like the 10-25 race's play date.
        out = _run(accept=set(), payload_overrides={
            "prestage_times": None, "time_start": "08:00", "time_end": "10:00",
            "play_date": "2026-11-01"})
    finally:
        grid_store.fetch_grid, grid_store.fetch_course_grids = real_fetch, real_course
    fired = sorted({t for t, _ in FIRES})
    if not fired:
        return _fail("no pre-staged holds fired at all")
    early = [t for t in fired if t < "09:00"]
    if early:
        return _fail(f"fired before Saturday's 09:00 floor: {early} (all fired: {fired})")
    told = [e for e in out["events"] if e.get("type") == "event" and e.get("stage") == "prestage_grid"
            and "Saturdays" in str(e.get("message"))]
    if not told:
        return _fail("no prestage_grid decision saying the floor was borrowed from Saturday")
    print(f"  ok: Sunday, no floor of its own -> Saturday's 09:00 -> fired {fired}")


if __name__ == "__main__":
    os.environ["SNIPER_RACE_TIMEOUT_SECONDS"] = "4"
    os.environ["SNIPER_PRESTAGE_BURST_SECONDS"] = "2"
    for fn in (test_targets_fire_in_parallel, test_polling_runs_during_the_burst,
               test_extra_holds_are_released, test_single_win_books_once,
               test_win_landing_mid_poll_beats_the_poll_path,
               test_fee_course_prestaged_win_ends_in_payment_link,
               test_disagreeing_grids_split_the_targets,
               test_holds_start_at_the_release_floor,
               test_weekday_without_a_floor_borrows_one):
        print(f"\n{fn.__name__}")
        try:
            fn()
        except Exception as exc:
            FAILURES += 1
            import traceback; traceback.print_exc()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
