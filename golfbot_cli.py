#!/usr/bin/env python3
"""Golfbot CLI with interactive selection, dry-run support, and structured run logging."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


def _parse_booking_url(url: str) -> tuple[str, str, str]:
    """Extract (base_url, schedule_id, course_id) from a ForeUp booking URL."""
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    parts = [p for p in parsed.path.split("/") if p]
    schedule_id = ""
    course_id = ""
    if "booking" in parts:
        ix = parts.index("booking")
        remaining = parts[ix + 1:]
        if len(remaining) >= 1:
            schedule_id = remaining[0]
        if len(remaining) >= 2:
            course_id = remaining[1]
    return base_url, schedule_id, course_id


def _fetch_inventory(
    base_url: str,
    schedule_id: str,
    course_id: str,
    play_date: date,
    players: int,
    holes: int,
    username: str,
    password: str,
) -> list[dict[str, Any]]:
    from src.discovery.foreup_client import ForeUpDiscoveryClient

    client = ForeUpDiscoveryClient(base_url=base_url)
    request_id = f"cli-{uuid.uuid4().hex[:8]}"

    if username and password:
        print(f"  Logging in as {username}...")
        client.bootstrap_session(username=username, password=password, schedule_id=schedule_id, course_id=course_id, request_id=request_id)
        print("  Login successful.")

    print(f"  Fetching tee times for {play_date} (schedule {schedule_id})...")
    slots = client.lookup_availability(
        schedule_id=schedule_id,
        date=play_date.isoformat(),
        players=players,
        holes=holes,
        request_id=request_id,
    )
    return slots


def _filter_inventory(inventory: list[dict[str, Any]], players: int, start: time, end: time) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for slot in inventory:
        slot_time_str = slot.get("time", "")[:5]
        try:
            slot_time = time.fromisoformat(slot_time_str)
        except ValueError:
            continue
        available = int(slot.get("available_spots", slot.get("spots", 0)))
        if available >= players and start <= slot_time <= end:
            matches.append(slot)
    return matches


def _choose_slot(slots: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not slots:
        return None

    print("\nMatching tee times:")
    for i, slot in enumerate(slots, start=1):
        fee = slot.get("green_fee", "?")
        print(f"  [{i}] {slot['time']} | spots={slot['available_spots']} | ${fee} | id={slot.get('tee_time_id', 'n/a')}")

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
    parser.add_argument("--foreup-url", default="", help="ForeUp booking URL (extracts base URL, schedule ID, course ID)")
    parser.add_argument("--schedule-id", default="", help="ForeUp schedule ID (overrides URL extraction)")
    parser.add_argument("--course-id", default="", help="ForeUp course ID")
    parser.add_argument("--play-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--players", required=True, type=int, help="Player count")
    parser.add_argument("--time-start", required=True, help="HH:MM")
    parser.add_argument("--time-end", required=True, help="HH:MM")
    parser.add_argument("--holes", type=int, default=18, choices=[9, 18], help="Number of holes (default: 18)")
    parser.add_argument("--username", default="", help="ForeUp username (or set FOREUP_USERNAME)")
    parser.add_argument("--password", default="", help="ForeUp password (or set FOREUP_PASSWORD)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = uuid.uuid4().hex[:12]
    logger = RunLogger(run_id)

    play_date = date.fromisoformat(args.play_date)
    t_start = _hhmm(args.time_start)
    t_end = _hhmm(args.time_end)

    base_url = os.getenv("FOREUP_BASE_URL", "https://foreupsoftware.com")
    schedule_id = args.schedule_id or os.getenv("FOREUP_SCHEDULE_ID", "")
    course_id = args.course_id or os.getenv("FOREUP_COURSE_ID", "")

    if args.foreup_url:
        url_base, url_sched, url_course = _parse_booking_url(args.foreup_url)
        base_url = url_base
        schedule_id = schedule_id or url_sched
        course_id = course_id or url_course

    username = args.username or os.getenv("FOREUP_USERNAME", "")
    password = args.password or os.getenv("FOREUP_PASSWORD", "")

    if not schedule_id:
        print("Error: No schedule_id provided. Use --foreup-url or --schedule-id.")
        return 1

    step_start = datetime.now().timestamp()
    try:
        inventory = _fetch_inventory(
            base_url=base_url,
            schedule_id=schedule_id,
            course_id=course_id,
            play_date=play_date,
            players=args.players,
            holes=args.holes,
            username=username,
            password=password,
        )
    except Exception as exc:
        print(f"Error fetching tee times: {exc}")
        logger.add_step("discovery", "fetch_failed", str(exc), step_start, datetime.now().timestamp())
        log_path = logger.write()
        print(f"Structured log: {log_path}")
        return 1

    matches = _filter_inventory(inventory, args.players, t_start, t_end)
    step_end = datetime.now().timestamp()
    logger.add_step("discovery", "found_candidates", f"Found {len(matches)} matching tee times (of {len(inventory)} total)", step_start, step_end)

    if not matches:
        print(f"\nNo tee times matched your criteria ({len(inventory)} total slots found).")
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

    logger.add_step("selection", "selected_slot", f"Selected {selected['time']} (id={selected.get('tee_time_id', 'n/a')})", step_end, datetime.now().timestamp())

    print(f"\nSelected: {selected['time']} | spots={selected['available_spots']} | ${selected.get('green_fee', '?')}")

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

    try:
        from src.booking.foreup_client import ForeUpBookingClient

        booking_client = ForeUpBookingClient(base_url=base_url)
        if username and password:
            booking_client.bootstrap_session(username=username, password=password, schedule_id=schedule_id, course_id=course_id)

        book_start = datetime.now().timestamp()
        response, latency = booking_client.book_slot(
            idempotency_key=f"cli-{run_id}-{selected.get('tee_time_id', 'unknown')}",
            slot=selected,
            schedule_id=schedule_id,
            date=args.play_date,
            players=args.players,
            holes=args.holes,
        )
        book_end = datetime.now().timestamp()
        logger.add_step("confirmation_gate", "sent_confirmation_request", f"Booking submitted ({latency.total_ms}ms)", book_start, book_end)
        logger.mark_confirmation_sent()
        logger.mark_success()
        print(f"Booking submitted! Response: {json.dumps(response, indent=2)}")
    except Exception as exc:
        logger.add_step("confirmation_gate", "booking_failed", str(exc), datetime.now().timestamp(), datetime.now().timestamp())
        print(f"Booking failed: {exc}")

    log_path = logger.write()
    print(f"Structured log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
