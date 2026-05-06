#!/usr/bin/env python3
"""Summarize Golfbot run logs from logs/*.json."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

LOG_DIR = Path("logs")


def main() -> int:
    files = sorted(LOG_DIR.glob("run_*.json"))
    if not files:
        print("No run logs found in logs/.")
        return 0

    attempts = len(files)
    successes = 0
    phase_totals: dict[str, list[int]] = defaultdict(list)

    for file in files:
        data = json.loads(file.read_text(encoding="utf-8"))
        if data.get("success"):
            successes += 1
        for point in data.get("race_loss_points", []):
            phase = point.get("phase", "unknown")
            elapsed_ms = int(point.get("elapsed_ms", 0))
            phase_totals[phase].append(elapsed_ms)

    success_rate = (successes / attempts) * 100 if attempts else 0.0

    print("Golfbot Run Summary")
    print(f"Attempts: {attempts}")
    print(f"Successes: {successes}")
    print(f"Success rate: {success_rate:.1f}%")
    print("Average race loss points (ms):")

    for phase, values in sorted(phase_totals.items()):
        avg = sum(values) / len(values)
        print(f"  - {phase}: {avg:.1f}ms")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
