"""Structured race telemetry for sniper runs.

A sniper race is decided in milliseconds, so when we lose we need to know
*where* we lost, not just that we did. The four losses look identical in the UI
today: we started polling after the release, we were served an anti-bot
challenge, the slot never appeared, or we saw it and lost the submit.

This module accumulates the evidence in memory during the race — no disk I/O on
the hot path — and flushes one structured record at the end.

That record goes to two places:

* **stdout**, as a single ``GOLFBOT_RACE`` JSON line. Render reclaims the
  container filesystem on every deploy and restart, so files under ``logs/``
  do not survive; the log stream does. This is the copy that is actually
  readable after a lost race in production.
* **``$GOLFBOT_LOG_DIR/run_<id>.json``** (default ``logs/``) for
  ``scripts/analyze_runs.py`` and local runs.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# A first poll landing more than this far after the release instant means the
# arming path — not the poll loop — is what cost us the race.
LATE_START_THRESHOLD_MS = 250.0

# Outcome taxonomy. Ordered loosely from best to worst so a human scanning a
# pile of runs can sort by it.
OUTCOME_WON = "won"
OUTCOME_FOUND_NOT_BOOKED = "found_not_booked"
OUTCOME_LOST_AT_SUBMIT = "lost_at_submit"
OUTCOME_NEVER_SAW_SLOT = "never_saw_slot"
OUTCOME_BLOCKED = "blocked"
OUTCOME_LATE_START = "late_start"
OUTCOME_ABORTED = "aborted"
OUTCOME_ERROR = "error"

_REASONS = {
    OUTCOME_WON: "Booked.",
    OUTCOME_FOUND_NOT_BOOKED: "Slot found but auto-book was off — nothing was submitted.",
    OUTCOME_LOST_AT_SUBMIT: (
        "We saw the slot and lost it during submit. The race is being lost "
        "between detection and confirmation, not in the poll loop."
    ),
    OUTCOME_NEVER_SAW_SLOT: (
        "Polling worked and was not blocked, but no matching slot ever "
        "appeared. Either it was taken before our first poll, or the filters "
        "did not match what was released."
    ),
    OUTCOME_BLOCKED: (
        "ForeUp served anti-bot responses during the race. Polls returned "
        "nothing because they were rejected, not because inventory was empty."
    ),
    OUTCOME_LATE_START: (
        "The first poll fired after the release instant. The arming path "
        "overran the release; poll speed is irrelevant until this is fixed."
    ),
    OUTCOME_ABORTED: "The race was stopped deliberately before it resolved.",
    OUTCOME_ERROR: "The race ended on an unhandled error.",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _percentile_ms(values: list[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile. Returns None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    # Nearest-rank: ceil(pct/100 * n), clamped into range.
    rank = int(-(-len(ordered) * pct // 100))
    return round(ordered[max(0, min(rank, len(ordered)) - 1)], 2)


@dataclass
class PollStats:
    count: int = 0
    empty: int = 0
    antibot: int = 0
    error: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "empty": self.empty,
            "antibot": self.antibot,
            "error": self.error,
            "p50_ms": _percentile_ms(self.latencies_ms, 50),
            "p95_ms": _percentile_ms(self.latencies_ms, 95),
            "max_ms": round(max(self.latencies_ms), 2) if self.latencies_ms else None,
        }


@dataclass
class RaceRecorder:
    """Collects sniper race telemetry. Cheap to call from the poll loop."""

    run_id: str
    job_id: str = ""
    mode: str = "sniper"
    course_ids: list[str] = field(default_factory=list)
    play_date: str = ""
    release_at: str = ""

    # --- arming ---
    rewarm_ms: Optional[float] = None
    rewarm_overran_release: bool = False
    # Milliseconds between the release instant and our first poll. Negative is
    # good (we were early and waiting); positive means we started late.
    first_poll_offset_ms: Optional[float] = None

    # --- race ---
    first_slot_seen_offset_ms: Optional[float] = None
    detect_to_submit_ms: Optional[float] = None
    submit_to_confirm_ms: Optional[float] = None
    total_race_ms: Optional[float] = None

    polls: PollStats = field(default_factory=PollStats)
    attempts: list[dict[str, Any]] = field(default_factory=list)

    captcha_solves: int = 0
    captcha_refreshes: int = 0
    captcha_stale_at_submit: bool = False

    booked: bool = False
    auto_book: bool = False
    aborted_reason: str = ""
    error: str = ""

    _started: float = field(default_factory=time.perf_counter)
    _release_monotonic: Optional[float] = None

    # ------------------------------------------------------------------
    # recording
    # ------------------------------------------------------------------
    def mark_release_instant(self, release_monotonic: float) -> None:
        """Anchor the release moment on the monotonic clock for offset math."""
        self._release_monotonic = release_monotonic

    def record_rewarm(self, duration_ms: float, overran: bool) -> None:
        self.rewarm_ms = round(duration_ms, 2)
        self.rewarm_overran_release = overran

    def record_first_poll(self, at_monotonic: float) -> None:
        if self.first_poll_offset_ms is not None or self._release_monotonic is None:
            return
        self.first_poll_offset_ms = round((at_monotonic - self._release_monotonic) * 1000, 2)

    def record_poll(self, latency_ms: float, slot_count: int, result: str = "ok") -> None:
        self.polls.count += 1
        self.polls.latencies_ms.append(latency_ms)
        if result == "antibot":
            self.polls.antibot += 1
        elif result == "error":
            self.polls.error += 1
        elif slot_count == 0:
            self.polls.empty += 1

    def record_slot_seen(self, at_monotonic: float) -> None:
        if self.first_slot_seen_offset_ms is not None or self._release_monotonic is None:
            return
        self.first_slot_seen_offset_ms = round((at_monotonic - self._release_monotonic) * 1000, 2)

    def record_attempt(self, slot_time: str, result: str, error: str = "", latency_ms: Optional[float] = None) -> None:
        self.attempts.append(
            {
                "slot_time": slot_time,
                "result": result,
                "error": error[:300],
                "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
                "at": _now_iso(),
            }
        )

    # ------------------------------------------------------------------
    # classification
    # ------------------------------------------------------------------
    def classify(self) -> str:
        if self.booked:
            return OUTCOME_WON
        if self.error:
            return OUTCOME_ERROR
        if self.aborted_reason:
            return OUTCOME_ABORTED
        # A late start explains everything downstream of it, so it wins the
        # classification even when other symptoms are present. Only meaningful
        # in timed sniper mode — snipe-now has no release to be late for, and
        # its offset is just "how long arming took".
        if self.mode == "sniper" and (self.first_poll_offset_ms or 0) > LATE_START_THRESHOLD_MS:
            return OUTCOME_LATE_START
        if self.polls.antibot > 0:
            return OUTCOME_BLOCKED
        if self.attempts:
            return OUTCOME_LOST_AT_SUBMIT
        if self.first_slot_seen_offset_ms is not None and not self.auto_book:
            return OUTCOME_FOUND_NOT_BOOKED
        return OUTCOME_NEVER_SAW_SLOT

    def summary_line(self) -> str:
        """One sentence a human can read in the UI without opening the JSON."""
        outcome = self.classify()
        reason = _REASONS.get(outcome, "")
        bits: list[str] = []
        if self.first_poll_offset_ms is not None and self.mode == "sniper":
            offset = self.first_poll_offset_ms
            when = "late" if offset > 0 else "early"
            bits.append(f"first poll {abs(offset):.0f}ms {when}")
        if self.polls.count:
            bits.append(f"{self.polls.count} polls")
        if self.polls.antibot:
            bits.append(f"{self.polls.antibot} anti-bot")
        if self.attempts:
            bits.append(f"{len(self.attempts)} submit attempts")
        detail = " | ".join(bits)
        return f"{outcome.upper()}: {reason}" + (f" [{detail}]" if detail else "")

    # ------------------------------------------------------------------
    # output
    # ------------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "kind": "sniper_race",
            "mode": self.mode,
            "recorded_at": _now_iso(),
            "course_ids": self.course_ids,
            "play_date": self.play_date,
            "release_at": self.release_at,
            "outcome": self.classify(),
            "summary": self.summary_line(),
            # analyze_runs.py compatibility
            "success": self.booked,
            "timing": {
                "rewarm_ms": self.rewarm_ms,
                "rewarm_overran_release": self.rewarm_overran_release,
                "first_poll_offset_ms": self.first_poll_offset_ms,
                "first_slot_seen_offset_ms": self.first_slot_seen_offset_ms,
                "detect_to_submit_ms": self.detect_to_submit_ms,
                "submit_to_confirm_ms": self.submit_to_confirm_ms,
                "total_race_ms": self.total_race_ms,
            },
            "polls": self.polls.as_dict(),
            "attempts": self.attempts,
            "captcha": {
                "solves": self.captcha_solves,
                "refreshes": self.captcha_refreshes,
                "stale_at_submit": self.captcha_stale_at_submit,
            },
            "aborted_reason": self.aborted_reason,
            "error": self.error,
        }

    def flush(self) -> dict[str, Any]:
        """Emit the race record. Never raises — telemetry must not kill a run."""
        if self.total_race_ms is None:
            self.total_race_ms = round((time.perf_counter() - self._started) * 1000, 2)
        record = self.as_dict()

        # stdout first: this is the copy that survives an ephemeral container.
        try:
            sys.stdout.write("GOLFBOT_RACE " + json.dumps(record, default=str) + "\n")
            sys.stdout.flush()
        except Exception:
            pass

        try:
            log_dir = Path(os.getenv("GOLFBOT_LOG_DIR", "logs"))
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / f"run_{self.run_id}.json").write_text(
                json.dumps(record, indent=2, default=str), encoding="utf-8"
            )
        except Exception:
            pass

        return record
