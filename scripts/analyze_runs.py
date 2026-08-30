#!/usr/bin/env python3
"""Summarize Golfbot run logs.

Reads two shapes out of logs/*.json:

* ``sniper_race`` records written by ``src/telemetry/race_log.py`` — these
  carry the loss classification and the arming-path timings.
* the older CLI ``race_loss_points`` records written by ``golfbot_cli.py``.

On Render the container filesystem is ephemeral, so the durable copy of a race
record is the ``GOLFBOT_RACE`` line in the service log stream. To analyze those,
pipe them in:

    render logs --tail 1000 | grep GOLFBOT_RACE | python3 scripts/analyze_runs.py -
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

LOG_DIR = Path("logs")

# Ordered worst-first: the top non-zero bucket is where to spend effort.
OUTCOME_ORDER = [
    "unconfirmed",
    "late_start",
    "blocked",
    "lost_at_submit",
    "never_saw_slot",
    "error",
    "aborted",
    "found_not_booked",
    "won",
]

NEXT_STEP = {
    "unconfirmed": "Submitted but ForeUp did not confirm a reservation. Check the "
                   "attempts[].response field on those rows and verify the account.",
    "late_start": "Arming path is overrunning the release. Raise SNIPER_PREWARM_LEAD_SECONDS.",
    "blocked": "ForeUp is rejecting polls. Back off the poll rate before tuning anything else.",
    "lost_at_submit": "Detection is fine; time is being lost between detect and confirm.",
    "never_saw_slot": "Check the time window and booking class actually match what is released.",
    "error": "Unhandled failures — read the error field on the individual records.",
    "aborted": "Races are being stopped deliberately (usually a missed 2FA code).",
    "found_not_booked": "Auto-book was off, so nothing was submitted.",
}


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _load_records(argv: list[str]) -> list[dict[str, Any]]:
    """Load from stdin when passed '-', otherwise from the local log dir."""
    records: list[dict[str, Any]] = []

    if len(argv) > 1 and argv[1] == "-":
        for line in sys.stdin:
            _, sep, payload = line.partition("GOLFBOT_RACE ")
            if not sep:
                continue
            try:
                records.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
        return records

    # An explicit directory wins — this is how you read logs pulled down from
    # the race-logs branch, which stores one file per race.
    log_dir = Path(argv[1]) if len(argv) > 1 else Path(os.getenv("GOLFBOT_LOG_DIR", "logs"))

    for jsonl in sorted(log_dir.rglob("*.jsonl")):
        try:
            lines = jsonl.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # One-file-per-race from the GitHub sink, plus older per-run CLI files.
    for file in sorted(log_dir.rglob("*.json")):
        try:
            records.append(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue

    return records


def _report_races(races: list[dict[str, Any]]) -> None:
    print(f"\nSniper races: {len(races)}")
    outcomes = Counter(r.get("outcome", "unknown") for r in races)
    won = outcomes.get("won", 0)
    print(f"Won: {won} ({won / len(races) * 100:.0f}%)\n")

    print("Why we lost:")
    for outcome in OUTCOME_ORDER:
        count = outcomes.get(outcome, 0)
        if not count or outcome == "won":
            continue
        print(f"  {count:>3}x {outcome:<18} {NEXT_STEP.get(outcome, '')}")
    for outcome, count in outcomes.items():
        if outcome not in OUTCOME_ORDER:
            print(f"  {count:>3}x {outcome:<18} (unrecognized)")

    # The single most diagnostic number: were we polling before the release?
    # Only timed sniper runs have a release to be measured against.
    timed = [r for r in races if r.get("mode") == "sniper"]
    offsets = [
        r["timing"]["first_poll_offset_ms"]
        for r in timed
        if isinstance(r.get("timing"), dict) and r["timing"].get("first_poll_offset_ms") is not None
    ]
    if offsets:
        late = [o for o in offsets if o > 0]
        med = _median(offsets)
        print("\nStart timing (negative = polling before release, good):")
        print(f"  median first-poll offset: {med:+.0f}ms")
        print(f"  worst: {max(offsets):+.0f}ms | best: {min(offsets):+.0f}ms")
        print(f"  races that started late: {len(late)}/{len(offsets)}")

    overruns = sum(
        1 for r in timed
        if isinstance(r.get("timing"), dict) and r["timing"].get("rewarm_overran_release")
    )
    if overruns:
        print(f"  re-warm overran the release in {overruns} race(s)")

    latencies = defaultdict(list)
    for r in races:
        timing = r.get("timing") or {}
        for key in ("rewarm_ms", "detect_to_submit_ms", "submit_to_confirm_ms"):
            if timing.get(key) is not None:
                latencies[key].append(timing[key])
        polls = r.get("polls") or {}
        if polls.get("p50_ms") is not None:
            latencies["poll_p50_ms"].append(polls["p50_ms"])

    if latencies:
        print("\nMedian latencies:")
        for key, values in latencies.items():
            print(f"  {key:<22} {_median(values):.0f}ms  (n={len(values)})")

    # Future Sight: how the pre-staged fires are doing across races.
    ps_races = [r for r in races if r.get("prestage")]
    if ps_races:
        fires = [p for r in ps_races for p in r["prestage"]]
        accepted = [p for p in fires if p.get("result") == "hold_accepted"]
        lat = [p["latency_ms"] for p in accepted if p.get("latency_ms") is not None]
        print(f"\nFuture Sight: {len(ps_races)} race(s) pre-staged, "
              f"{len(fires)} fires, {len(accepted)} holds accepted")
        if lat:
            print(f"  accepted-hold latency: median {_median(lat):.0f}ms")
        reasons = Counter(
            (p.get("error") or "").split("|")[-1].strip()[:60]
            for p in fires if p.get("result") == "rejected"
        )
        for reason, count in reasons.most_common(3):
            print(f"  {count:>3}x rejected: {reason or '(no body)'}")

    # Self-collision: a loss with peers armed on identical criteria may have
    # been to one of our own users, which is a completely different problem
    # from losing to a stranger's bot and has a completely different fix.
    contested = [r for r in races if (r.get("peers") or {}).get("count", 1) > 1]
    if contested:
        lost = [r for r in contested if r.get("outcome") not in ("won", "found_not_booked")]
        print(f"\nSelf-collision: {len(contested)}/{len(races)} races ran with other "
              f"jobs of ours on identical criteria")
        biggest = max((r.get("peers") or {}).get("count", 1) for r in contested)
        print(f"  most concurrent jobs on one race: {biggest}")
        if lost:
            print(f"  {len(lost)} of those did not win — check whether the winning time "
                  f"went to another of our own runs before blaming an outside bot")
        # If rotation is working, the opening picks should be spread across
        # ordinals rather than every job sitting on 0.
        ordinals = Counter((r.get("peers") or {}).get("ordinal", 0) for r in contested)
        spread = ", ".join(f"#{o}:{n}" for o, n in sorted(ordinals.items()))
        print(f"  rotation ordinals seen: {spread}")
        if len(ordinals) == 1 and biggest > 1:
            print("  WARNING: every contested race used the same ordinal — rotation is "
                  "not taking effect, so our users are still dogpiling one slot.")

    blocked_polls = sum((r.get("polls") or {}).get("antibot", 0) for r in races)
    if blocked_polls:
        print(f"\nAnti-bot rejected polls across all races: {blocked_polls}")

    stale = sum(1 for r in races if (r.get("captcha") or {}).get("stale_at_submit"))
    if stale:
        print(f"Races that reached submit with a stale captcha token: {stale}")


def _report_cli_runs(runs: list[dict[str, Any]]) -> None:
    successes = sum(1 for r in runs if r.get("success"))
    phase_totals: dict[str, list[float]] = defaultdict(list)
    for run in runs:
        for point in run.get("race_loss_points", []):
            phase_totals[point.get("phase", "unknown")].append(float(point.get("elapsed_ms", 0)))

    print(f"\nCLI runs: {len(runs)}")
    print(f"Successes: {successes} ({successes / len(runs) * 100:.0f}%)")
    if phase_totals:
        print("Average race loss points (ms):")
        for phase, values in sorted(phase_totals.items()):
            print(f"  - {phase}: {sum(values) / len(values):.1f}ms")


def main(argv: list[str]) -> int:
    records = _load_records(argv)
    if not records:
        print("No run logs found. Locally: logs/run_*.json. On Render, pipe the")
        print("service log stream in:  ... | grep GOLFBOT_RACE | analyze_runs.py -")
        return 0

    races = [r for r in records if r.get("kind") == "sniper_race"]
    cli_runs = [r for r in records if r.get("kind") != "sniper_race"]

    print("Golfbot Run Summary")
    if races:
        _report_races(races)
    if cli_runs:
        _report_cli_runs(cli_runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
