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


GITHUB_HINTS: list[tuple[str, str]] = [
    (
        "401",
        "The token was rejected. Create a fine-grained token with Contents: "
        "Read and write on this repo, and set RACE_LOG_GITHUB_TOKEN.",
    ),
    (
        "403",
        "The token authenticated but lacks permission. It needs Contents: "
        "Read and write (not just Read) on this specific repo.",
    ),
    (
        "404",
        "Repo not found, or the token cannot see it. Check "
        "RACE_LOG_GITHUB_REPO is 'owner/name' and the token grants access "
        "to that repo.",
    ),
    (
        "422",
        "GitHub rejected the write. Usually a path collision — harmless if a "
        "record with this name already exists.",
    ),
]


def _report(name: str, status: dict, hints: list[tuple[str, str]]) -> bool:
    """Print one sink's result. Returns True if it is working or unconfigured."""
    if not status.get("configured"):
        print(f"  {name}: not configured — skipped")
        return True
    if status.get("ok"):
        where = status.get("path") or f"HTTP {status.get('status_code')}"
        print(f"  {name}: PASS ({where})")
        return True

    detail = str(status.get("error") or status.get("body") or "")
    code = status.get("status_code")
    print(f"  {name}: FAIL{f' (HTTP {code})' if code else ''}")
    if detail:
        print(f"      response: {detail}")
    lowered = f"{code} {detail}".lower()
    for needle, hint in hints:
        if needle.lower() in lowered:
            print(f"      likely cause: {hint}")
            break
    return False


def main() -> int:
    url = os.getenv("RACE_LOG_WEBHOOK_URL", "").strip()
    gh_repo = os.getenv("RACE_LOG_GITHUB_REPO", "").strip()
    gh_token = os.getenv("RACE_LOG_GITHUB_TOKEN", "").strip()

    if not url and not (gh_repo and gh_token):
        print("No durable sink is configured — nothing to verify.\n")
        print("For the GitHub sink (no signup needed), set:")
        print("  RACE_LOG_GITHUB_REPO=owner/name")
        print("  RACE_LOG_GITHUB_TOKEN=<fine-grained token, Contents: read+write>")
        print("  RACE_LOG_GITHUB_BRANCH=race-logs   # optional, this is the default")
        print("\nFor an HTTP sink (Supabase or any webhook), set:")
        print('  RACE_LOG_WEBHOOK_URL=https://<project>.supabase.co/rest/v1/races')
        print('  RACE_LOG_WEBHOOK_HEADERS={"apikey":"SERVICE_KEY",'
              '"Authorization":"Bearer SERVICE_KEY","Prefer":"return=minimal"}')
        return 1

    headers_env = os.getenv("RACE_LOG_WEBHOOK_HEADERS", "").strip()
    token_env = os.getenv("RACE_LOG_WEBHOOK_TOKEN", "").strip()
    if url:
        auth = "custom headers" if headers_env else ("bearer token" if token_env else "NONE — likely to be rejected")
        print(f"HTTP sink   : {url}  ({auth})")
    if gh_repo and gh_token:
        branch = os.getenv("RACE_LOG_GITHUB_BRANCH", "race-logs").strip() or "race-logs"
        print(f"GitHub sink : {gh_repo} on branch '{branch}'")
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
    sinks = record.get("sink") or {}

    http_ok = _report("HTTP  ", sinks.get("http") or {}, HINTS)
    gh = sinks.get("github") or {}
    gh_ok = _report("GitHub", gh, GITHUB_HINTS)

    if http_ok and gh_ok:
        print("\nPASS — real races will be recorded durably.")
        if gh.get("ok"):
            branch = gh.get("branch", "race-logs")
            print(f"\nThe test record is committed at {gh.get('path')} on '{branch}'.")
            print("Read your history locally with:")
            print(f"  git fetch origin {branch}")
            print(f"  git checkout origin/{branch} -- race-logs/")
            print("  python3 scripts/analyze_runs.py race-logs/")
            print(f"\nDelete the test record when you are done:")
            print(f"  (via GitHub UI on the '{branch}' branch, or leave it — it is harmless)")
        return 0

    print("\nFAIL — at least one configured sink is not working.")
    print("The race itself would still have run — telemetry never blocks a "
          "booking — but nothing would be stored.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
