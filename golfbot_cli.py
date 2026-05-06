#!/usr/bin/env python3
"""Golfbot CLI with dry-run support and structured run logging."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_DIR = Path("logs")


@dataclass
class Decision:
    phase: str
    decision: str
    reason: str
    elapsed_ms: int


class RunLogger:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.started_at = datetime.now(timezone.utc)
        self.decisions: list[Decision] = []
        self.latency_points: list[dict[str, Any]] = []
        self.success = False
        self.confirmation_sent = False

    def add_step(self, phase: str, decision: str, reason: str, start: float, end: float) -> None:
        elapsed_ms = int((end - start) * 1000)
        self.decisions.append(
            Decision(phase=phase, decision=decision, reason=reason, elapsed_ms=elapsed_ms)
        )
        self.latency_points.append({"phase": phase, "elapsed_ms": elapsed_ms})

    def mark_success(self) -> None:
        self.success = True

    def mark_confirmation_sent(self) -> None:
        self.confirmation_sent = True

    def write(self) -> Path:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        output = {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "success": self.success,
            "confirmation_sent": self.confirmation_sent,
            "decision_trace": [d.__dict__ for d in self.decisions],
            "race_loss_points": self.latency_points,
        }
        path = LOG_DIR / f"run_{self.run_id}.json"
        path.write_text(json.dumps(output, indent=2), encoding="utf-8")
        return path


def simulate_work(seconds: float) -> None:
    time.sleep(seconds)


def perform_flow(dry_run: bool) -> tuple[bool, bool, RunLogger]:
    run_id = uuid.uuid4().hex[:12]
    logger = RunLogger(run_id)

    start = time.perf_counter()
    simulate_work(0.03)
    end = time.perf_counter()
    logger.add_step("discovery", "selected_inventory", "Found 3 candidate tee times", start, end)

    start = time.perf_counter()
    simulate_work(0.04)
    end = time.perf_counter()
    logger.add_step("booking_prep", "chose_best_option", "Lowest points + preferred window", start, end)

    if dry_run:
        start = time.perf_counter()
        simulate_work(0.01)
        end = time.perf_counter()
        logger.add_step(
            "confirmation_gate",
            "skipped_confirmation",
            "Dry-run enabled; stopping before irreversible action",
            start,
            end,
        )
        logger.mark_success()
        return True, False, logger

    start = time.perf_counter()
    simulate_work(0.03)
    end = time.perf_counter()
    logger.add_step("confirmation_gate", "sent_confirmation_request", "Ready for irreversible confirmation", start, end)
    logger.mark_confirmation_sent()
    logger.mark_success()
    return True, True, logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Golfbot CLI")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform discovery and booking preparation, then stop before confirmation request.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    success, confirmation_sent, logger = perform_flow(args.dry_run)
    log_path = logger.write()

    print(f"Run complete: success={success} dry_run={args.dry_run} confirmation_sent={confirmation_sent}")
    print(f"Structured log: {log_path}")

    if args.dry_run:
        print("Stopped before irreversible confirmation request (--dry-run).")

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
