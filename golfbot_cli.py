#!/usr/bin/env python3
"""Golfbot CLI with interactive selection, dry-run support, and structured run logging."""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
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


def _hhmm(value: str) -> time:
    return time.fromisoformat(value)


def _sample_inventory(course_id: str, play_date: date) -> list[dict[str, Any]]:
    return [
        {"course_id": course_id, "tee_time_id": f"{course_id}-{play_date}-1", "starts_at": "07:10", "spots": 4, "price": 129.00},
        {"course_id": course_id, "tee_time_id": f"{course_id}-{play_date}-2", "starts_at": "07:40", "spots": 2, "price": 149.00},
        {"course_id": course_id, "tee_time_id": f"{course_id}-{play_date}-3", "starts_at": "08:20", "spots": 4, "price": 139.00},
        {"course_id": course_id, "tee_time_id": f"{course_id}-{play_date}-4", "starts_at": "09:10", "spots": 4, "price": 119.00},
    ]


def _filter_inventory(inventory: list[dict[str, Any]], players: int, start: time, end: time) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for slot in inventory:
        slot_time = time.fromisoformat(slot["starts_at"])
        if slot["spots"] >= players and start <= slot_time <= end:
            matches.append(slot)
    return matches


def _choose_slot(slots: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not slots:
        return None

    print("\nMatching tee times:")
    for i, slot in enumerate(slots, start=1):
        print(f"  [{i}] {slot['starts_at']} | spots={slot['spots']} | ${slot['price']:.2f} | id={slot['tee_time_id']}")

    while True:
        selected = input("Pick a tee time number (or 'q' to cancel): ").strip().lower()
        if selected == "q":
            return None
        if selected.isdigit():
            idx = int(selected)
            if 1 <= idx <= len(slots):
                return slots[idx - 1]
        print("Invalid selection. Try again.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Golfbot interactive CLI")
    parser.add_argument("--dry-run", action="store_true", help="Stop before sending confirmation request.")
    parser.add_argument("--course-id", required=True, help="Course identifier")
    parser.add_argument("--play-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--players", required=True, type=int, help="Player count")
    parser.add_argument("--time-start", required=True, help="HH:MM")
    parser.add_argument("--time-end", required=True, help="HH:MM")
    parser.add_argument("--payment-profile-id", default="demo-profile", help="Payment profile id")
    parser.add_argument("--contact-email", default="demo@example.com", help="Contact email")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = uuid.uuid4().hex[:12]
    logger = RunLogger(run_id)

    play_date = date.fromisoformat(args.play_date)
    t_start = _hhmm(args.time_start)
    t_end = _hhmm(args.time_end)

    step_start = datetime.now().timestamp()
    inventory = _sample_inventory(args.course_id, play_date)
    matches = _filter_inventory(inventory, args.players, t_start, t_end)
    step_end = datetime.now().timestamp()
    logger.add_step("discovery", "found_candidates", f"Found {len(matches)} matching tee times", step_start, step_end)

    if not matches:
        print("No tee times matched your criteria.")
        log_path = logger.write()
        print(f"Structured log: {log_path}")
        return 1

    selected = _choose_slot(matches)
    if not selected:
        logger.add_step("selection", "cancelled", "User cancelled selection", step_end, datetime.now().timestamp())
        log_path = logger.write()
        print("Selection cancelled.")
        print(f"Structured log: {log_path}")
        return 1

    logger.add_step("selection", "selected_slot", f"Selected {selected['tee_time_id']}", step_end, datetime.now().timestamp())

    print("\nPreparing booking request:")
    print({"tee_time": selected, "payment_profile_id": args.payment_profile_id, "contact_email": args.contact_email})

    if args.dry_run:
        logger.add_step("confirmation_gate", "skipped_confirmation", "Dry-run enabled", datetime.now().timestamp(), datetime.now().timestamp())
        logger.mark_success()
        log_path = logger.write()
        print("Dry-run complete. No booking submitted.")
        print(f"Structured log: {log_path}")
        return 0

    confirm = input("Send booking confirmation request now? (y/N): ").strip().lower()
    if confirm != "y":
        logger.add_step("confirmation_gate", "aborted", "User chose not to confirm", datetime.now().timestamp(), datetime.now().timestamp())
        log_path = logger.write()
        print("Booking aborted by user.")
        print(f"Structured log: {log_path}")
        return 1

    logger.add_step("confirmation_gate", "sent_confirmation_request", "User confirmed submission", datetime.now().timestamp(), datetime.now().timestamp())
    logger.mark_confirmation_sent()
    logger.mark_success()
    log_path = logger.write()
    print("Booking request submitted (simulated).")
    print(f"Structured log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
