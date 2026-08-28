#!/usr/bin/env python3
"""Send one synthetic race record through the real sink and report what happened.

Run this after wiring up RACE_LOG_WEBHOOK_URL, so a broken sink surfaces now
rather than the day you go looking for a month of history and find it empty.

    python3 scripts/verify_race_sink.py

It uses the same code path as a real race, so a pass here means a real race will
land too. The test row is written with a ``verify-`` run_id prefix; the script
prints the SQL to remove it.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.telemetry.race_log import RaceRecorder  # noqa: E402

# Maps the failures you are actually likely to hit to what to do about them,
# because "400 Bad Request" on its own does not tell you which of five setup
# steps was missed.
HINTS: list[tuple[str, str]] = [
    (
        "does not exist",
        "The table or a column is missing. Run supabase/schema.sql in the "
        "Supabase SQL editor. If it names a specific column, the record gained "
        "a field the table does not have yet — add it as jsonb/text.",
    ),
    (
        "row-level security",
        "You are using the anon key. Race telemetry writes need the "
        "service_role key (Project Settings -> API -> service_role). It "
        "bypasses RLS; the anon key does not.",
    ),
    (
        "Invalid API key",
        "The apikey/Authorization headers are wrong. Both must be set to the "
        "service_role key. Check RACE_LOG_WEBHOOK_HEADERS is valid JSON.",
    ),
    (
        "JWT",
        "The key was rejected. Copy the service_role key again — it is easy to "
        "grab a truncated value from the dashboard.",
    ),
    (
        "404",
        "The URL is wrong. It should end in /rest/v1/<table>, e.g. "
        "https://<project>.supabase.co/rest/v1/races",
    ),
    (
        "ConnectError",
        "Could not reach the host at all. Check the project URL and that you "
        "have outbound network access from wherever this is running.",
    ),
    (
        "Timeout",
        "The endpoint accepted the connection but did not answer within 5s. A "
        "real race will not block on this, but the record would be lost.",
    ),
]


def _hint_for(text: str) -> str:
    lowered = text.lower()
    for needle, hint in HINTS:
        if needle.lower() in lowered:
            return hint
    return ""


def main() -> int:
    url = os.getenv("RACE_LOG_WEBHOOK_URL", "").strip()
    if not url:
        print("RACE_LOG_WEBHOOK_URL is not set — nothing to verify.\n")
        print("Set these in .env (locally) or Render -> Environment:")
        print("  RACE_LOG_WEBHOOK_URL=https://<project>.supabase.co/rest/v1/races")
        print('  RACE_LOG_WEBHOOK_HEADERS={"apikey":"SERVICE_KEY",'
              '"Authorization":"Bearer SERVICE_KEY","Prefer":"return=minimal"}')
        print("\nUse the service_role key from Supabase -> Project Settings -> API,")
        print("not the anon key and not the database password.")
        return 1

    headers_env = os.getenv("RACE_LOG_WEBHOOK_HEADERS", "").strip()
    token_env = os.getenv("RACE_LOG_WEBHOOK_TOKEN", "").strip()
    auth = "custom headers" if headers_env else ("bearer token" if token_env else "NONE — will be rejected")
    print(f"Sink : {url}")
    print(f"Auth : {auth}")
    print()

    run_id = f"verify-{int(time.time())}"
    race = RaceRecorder(
        run_id=run_id,
        job_id="verify",
        mode="sniper",
        course_ids=["verify"],
        play_date="1970-01-01",
        release_at="1970-01-01T00:00:00+00:00",
        auto_book=False,
    )
    now = time.perf_counter()
    race.mark_release_instant(now)
    race.record_rewarm(1234.0, False)
    race.record_first_poll(now - 0.05)
    race.record_poll(95.0, 0)
    race.record_attempt("07:00am", "failed", error="synthetic verification record")

    print(f"Sending synthetic record run_id={run_id} ...\n")
    record = race.flush()
    sink = record.get("sink") or {}

    if sink.get("ok"):
        print(f"PASS — Supabase accepted the record (HTTP {sink.get('status_code')}).")
        print("\nConfirm it landed, in the Supabase SQL editor:")
        print(f"  select * from races where run_id = '{run_id}';")
        print("\nThen remove the test row:")
        print(f"  delete from races where run_id = '{run_id}';")
        print("\nReal races will now be recorded durably.")
        return 0

    detail = str(sink.get("error") or sink.get("body") or "")
    status = sink.get("status_code")
    print(f"FAIL — the sink rejected the record{f' (HTTP {status})' if status else ''}.")
    if detail:
        print(f"\nResponse: {detail}")
    hint = _hint_for(f"{status} {detail}")
    if hint:
        print(f"\nLikely cause: {hint}")
    print("\nThe race itself would still have run — telemetry never blocks a "
          "booking — but nothing would be stored.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
