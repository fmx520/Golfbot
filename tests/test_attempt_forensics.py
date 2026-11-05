"""When ForeUp refuses a hold, record enough to tell WHY without guessing.

2026-11-04: the availability API listed 09:50, 11:00 and 11:10 as open, and
pending_reservation refused all three with "Time not available." — the same
six-word string it uses for times that are not on the sheet at all. With only
that string there is nothing to reason from, so the slot as advertised is now
recorded next to the payload actually sent, and the obvious disagreements are
named.
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


def _rec():
    return RaceRecorder(run_id="t", job_id="j", mode="sniper", course_ids=["2431"],
                        play_date="2026-11-11", release_at="", auto_book=True)


def check(name, cond, detail=""):
    global FAILURES
    if cond:
        print(f"  ok: {name}")
    else:
        FAILURES += 1
        print(f"FAIL: {name} {detail}")


SLOT = {"time": "2026-11-11 09:50", "available_spots": 4, "green_fee": 80,
        "raw": {"booking_class_id": 50294, "teesheet_side_id": 1014,
                "schedule_id": 2431, "course_id": 19765, "available_spots_18": 4,
                "minimum_players": 1, "allowed_group_sizes": ["1", "2", "3", "4"]}}


def _sent(**over):
    base = {"time": "2026-11-11 09:50", "players": 4, "holes": 18,
            "schedule_id": 2431, "course_id": 19765,
            "booking_class_id": "50294", "teesheet_side_id": "1014"}
    base.update(over)
    return base


def test_records_both_sides_of_the_refusal():
    r = _rec()
    r.record_attempt("09:50", "failed", error='Body: {"msg":"Time not available."}',
                     slot_polled=SLOT, payload_sent=_sent())
    a = r.attempts[0]
    check("slot as advertised kept", a.get("slot_polled", {}).get("booking_class_id") == 50294, a)
    check("payload we sent kept", a.get("payload_sent", {}).get("players") == 4, a)
    check("matching request shows no mismatch", a.get("mismatch") == [], a.get("mismatch"))


def test_booking_class_disagreement_is_named():
    r = _rec()
    r.record_attempt("09:50", "failed", error="x", slot_polled=SLOT,
                     payload_sent=_sent(booking_class_id="2137"))
    m = r.attempts[0]["mismatch"]
    check("names the class mismatch",
          any("booking_class_id" in x and "2137" in x for x in m), m)


def test_asking_for_more_players_than_spots_is_named():
    thin = {**SLOT, "raw": {**SLOT["raw"], "available_spots_18": 2}}
    r = _rec()
    r.record_attempt("09:50", "failed", error="x", slot_polled=thin, payload_sent=_sent(players=4))
    m = r.attempts[0]["mismatch"]
    check("names the spots shortfall", any("asked 4" in x and "2 spot" in x for x in m), m)


def test_group_size_not_allowed_is_named():
    pairs = {**SLOT, "raw": {**SLOT["raw"], "allowed_group_sizes": ["2", "4"]}}
    r = _rec()
    r.record_attempt("09:50", "failed", error="x", slot_polled=pairs, payload_sent=_sent(players=3))
    m = r.attempts[0]["mismatch"]
    check("names the disallowed group size",
          any("allowed_group_sizes" in x for x in m), m)


def test_side_id_disagreement_is_named():
    r = _rec()
    r.record_attempt("09:50", "failed", error="x", slot_polled=SLOT,
                     payload_sent=_sent(teesheet_side_id="1016"))
    m = r.attempts[0]["mismatch"]
    check("names the side mismatch", any("teesheet_side_id" in x for x in m), m)


def test_prestage_keeps_what_it_sent():
    """The burst builds from a template, so the payload is the only evidence."""
    r = _rec()
    r.record_prestage("08:30", "rejected", 600.0, error='{"msg":"Time not available."}',
                      payload_sent=_sent(time="2026-11-11 08:30"))
    p = r.prestage[0]
    check("burst payload kept", p.get("payload_sent", {}).get("time") == "2026-11-11 08:30", p)


def test_absent_forensics_do_not_add_keys():
    r = _rec()
    r.record_attempt("09:50", "failed", error="x")
    a = r.attempts[0]
    check("no empty keys on a bare attempt",
          "slot_polled" not in a and "payload_sent" not in a and "mismatch" not in a, a)


if __name__ == "__main__":
    for fn in (test_records_both_sides_of_the_refusal,
               test_booking_class_disagreement_is_named,
               test_asking_for_more_players_than_spots_is_named,
               test_group_size_not_allowed_is_named,
               test_side_id_disagreement_is_named,
               test_prestage_keeps_what_it_sent,
               test_absent_forensics_do_not_add_keys):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
