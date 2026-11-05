"""Structured race telemetry for sniper runs.

A sniper race is decided in milliseconds, so when we lose we need to know
*where* we lost, not just that we did. The four losses look identical in the UI
today: we started polling after the release, we were served an anti-bot
challenge, the slot never appeared, or we saw it and lost the submit.

This module accumulates the evidence in memory during the race — no disk I/O on
the hot path — and flushes one structured record at the end.

That record goes to three places, cheapest and least durable first:

* **stdout**, as a single ``GOLFBOT_RACE`` JSON line. Free enough to always do,
  and it is the fallback when the network sink below fails.
* **``$GOLFBOT_LOG_DIR/races.jsonl``** (default ``logs/``), appended one record
  per line. Useful locally; on Render this dies with the container.
* **an HTTP sink**, when ``RACE_LOG_WEBHOOK_URL`` is set. This is the only copy
  that survives. Neither of the above is a basis for correlating races across
  weeks: Render's container filesystem is reclaimed on every deploy, restart,
  and free-plan spin-down, and the log stream — while held off-container — ages
  out on a retention window measured in days.

The sink is a plain JSON POST rather than a vendor SDK, so it works against
Supabase, a Firestore-fronting Cloud Function, or any webhook, and adds no
dependency or cold-start cost. It is best-effort by design: a telemetry failure
must never affect a booking.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Hard ceiling on the sink POST. The race is already decided by the time this
# runs, but the worker should not hang on a slow endpoint during teardown.
SINK_TIMEOUT_SECONDS = 5.0

# A first poll landing more than this far after the release instant means the
# arming path — not the poll loop — is what cost us the race.
LATE_START_THRESHOLD_MS = 250.0

# Outcome taxonomy. Ordered loosely from best to worst so a human scanning a
# pile of runs can sort by it.
OUTCOME_WON = "won"
OUTCOME_UNCONFIRMED = "unconfirmed"
OUTCOME_FOUND_NOT_BOOKED = "found_not_booked"
OUTCOME_LOST_AT_SUBMIT = "lost_at_submit"
OUTCOME_BLOCKED_BY_FEE = "blocked_by_fee"
OUTCOME_AWAITING_PAYMENT = "awaiting_payment"
OUTCOME_NEVER_SAW_SLOT = "never_saw_slot"
OUTCOME_BLOCKED = "blocked"
OUTCOME_LATE_START = "late_start"
OUTCOME_ABORTED = "aborted"
OUTCOME_ERROR = "error"

_REASONS = {
    OUTCOME_WON: "Booked.",
    OUTCOME_UNCONFIRMED: (
        "The booking was submitted and ForeUp answered 200, but the response "
        "carried no reservation we could identify. Check the course account "
        "before assuming you have the tee time."
    ),
    OUTCOME_FOUND_NOT_BOOKED: "Slot found but auto-book was off — nothing was submitted.",
    OUTCOME_AWAITING_PAYMENT: (
        "We won the hold and handed back a payment link. The race was won; "
        "whether it became a tee time depends on the fee being paid before "
        "the hold expires."
    ),
    OUTCOME_BLOCKED_BY_FEE: (
        "We WON the hold and could not convert it: the course requires a "
        "booking fee at checkout and the reservation payload carries no "
        "payment. This is not a speed problem — arriving earlier cannot fix it."
    ),
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


# Where the container's CPU-throttling counters live. cgroup v2 first (one
# unified cpu.stat), then the v1 layouts. The container's counters cover every
# process in it -- the web server and the race worker together, which is what
# shares the CPU quota.
_CGROUP_V2_STAT = "/sys/fs/cgroup/cpu.stat"
_CGROUP_V1_STATS = ("/sys/fs/cgroup/cpu/cpu.stat", "/sys/fs/cgroup/cpu,cpuacct/cpu.stat")
_CGROUP_V1_USAGE = ("/sys/fs/cgroup/cpuacct/cpuacct.usage", "/sys/fs/cgroup/cpu,cpuacct/cpuacct.usage")


def _read_kv(path: str) -> dict[str, int]:
    out: dict[str, int] = {}
    with open(path) as fh:
        for line in fh:
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                out[parts[0]] = int(parts[1])
    return out


def read_cpu_throttle(
    v2_stat: str = _CGROUP_V2_STAT,
    v1_stats: tuple[str, ...] = _CGROUP_V1_STATS,
    v1_usage: tuple[str, ...] = _CGROUP_V1_USAGE,
) -> Optional[dict[str, int]]:
    """Snapshot the kernel's CPU-throttle counters for this container.

    Returns {periods, throttled_periods, throttled_usec, usage_usec} -- usage
    may be absent -- or None where the counters cannot be read (a laptop, a
    non-Linux host). Never raises: this is measurement, and a race must not
    depend on it.

    Why it exists: STATE.md has argued both ways about whether our 0.15-CPU
    plan slows the release instant down. Render's metrics average over 30s,
    which hides a sub-second burst. These counters do not: the kernel bumps
    throttled_periods every 100ms window in which we hit the quota and had to
    wait. Zero across a race settles it.
    """
    try:
        if os.path.exists(v2_stat):
            kv = _read_kv(v2_stat)
            if "nr_periods" in kv:
                snap = {
                    "periods": kv.get("nr_periods", 0),
                    "throttled_periods": kv.get("nr_throttled", 0),
                    "throttled_usec": kv.get("throttled_usec", 0),
                }
                if "usage_usec" in kv:
                    snap["usage_usec"] = kv["usage_usec"]
                return snap
        for path in v1_stats:
            if not os.path.exists(path):
                continue
            kv = _read_kv(path)
            snap = {
                "periods": kv.get("nr_periods", 0),
                "throttled_periods": kv.get("nr_throttled", 0),
                # v1 reports nanoseconds.
                "throttled_usec": kv.get("throttled_time", 0) // 1000,
            }
            for upath in v1_usage:
                if os.path.exists(upath):
                    with open(upath) as fh:
                        snap["usage_usec"] = int(fh.read().strip() or 0) // 1000
                    break
            return snap
    except Exception:  # noqa: BLE001 -- measurement must never break a race
        return None
    return None


def cpu_throttle_delta(before: Optional[dict[str, int]],
                       after: Optional[dict[str, int]],
                       seconds: float) -> Optional[dict[str, Any]]:
    """What happened to our CPU between two snapshots, in readable units."""
    if not before or not after:
        return None
    out: dict[str, Any] = {
        "seconds": round(seconds, 2),
        "periods": after["periods"] - before["periods"],
        "throttled_periods": after["throttled_periods"] - before["throttled_periods"],
        "throttled_ms": round((after["throttled_usec"] - before["throttled_usec"]) / 1000, 1),
    }
    if "usage_usec" in before and "usage_usec" in after:
        out["cpu_used_ms"] = round((after["usage_usec"] - before["usage_usec"]) / 1000, 1)
    return out


def _sink_headers() -> dict[str, str]:
    """Build sink headers from env.

    ``RACE_LOG_WEBHOOK_HEADERS`` takes a JSON object for backends that need
    several (Supabase wants ``apikey``, ``Authorization`` and ``Prefer``).
    ``RACE_LOG_WEBHOOK_TOKEN`` is the shorthand for a plain bearer token.
    """
    headers = {"Content-Type": "application/json"}

    raw = os.getenv("RACE_LOG_WEBHOOK_HEADERS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                headers.update({str(k): str(v) for k, v in parsed.items()})
        except (ValueError, TypeError):
            # A malformed header env var must not take the sink down; the
            # request still goes out and will fail loudly at the endpoint.
            pass

    # A single key is enough for Supabase, so the JSON form above is optional.
    # Supabase wants the key in `apikey`, PostgREST accepts it as a bearer
    # token, and sending both is harmless — so send both rather than making the
    # caller know which. Anything set explicitly above still wins.
    token = os.getenv("RACE_LOG_WEBHOOK_TOKEN", "").strip()
    if token:
        headers.setdefault("Authorization", f"Bearer {token}")
        headers.setdefault("apikey", token)
        # Tells PostgREST not to echo the inserted row back. Ignored by
        # endpoints that do not understand it.
        headers.setdefault("Prefer", "return=minimal")

    return headers


_SINK_MAX_DRIFT_RETRIES = 6

_PGRST_MISSING_COLUMN = re.compile(
    r"Could not find the '([^']+)' column", re.IGNORECASE
)


def _missing_column(body: str) -> str:
    """Pull the column name out of a PostgREST PGRST204 error body."""
    match = _PGRST_MISSING_COLUMN.search(body or "")
    return match.group(1) if match else ""


def _post_to_sink(record: dict[str, Any]) -> dict[str, Any]:
    """POST the record to the durable sink. Best effort, never raises.

    Runs after the stdout and file writes so a hanging endpoint can never cost
    us the record entirely. A failure writes its own ``GOLFBOT_RACE_SINK_ERROR``
    line and is returned to the caller for display in the UI — a silently
    broken sink should surface now, not weeks later when the history you went
    to read turns out to be empty.
    """
    url = os.getenv("RACE_LOG_WEBHOOK_URL", "").strip()
    if not url:
        return {"configured": False}

    try:
        import httpx  # already a dependency; imported late to keep startup lean

        headers = _sink_headers()
        payload = dict(record)
        dropped: list[str] = []

        # PostgREST maps top-level keys to columns and rejects the ENTIRE insert
        # if one is unmapped (PGRST204). So shipping a new telemetry field before
        # its migration lands does not lose that field -- it loses the whole race
        # record, silently, for every run until someone notices. That has already
        # happened to this project once.
        #
        # Retry without the offending column instead. A record missing one field
        # is worth infinitely more than no record, and the dropped names are
        # printed so the missing migration is obvious rather than invisible.
        for _ in range(_SINK_MAX_DRIFT_RETRIES):
            response = httpx.post(
                url, json=payload, headers=headers, timeout=SINK_TIMEOUT_SECONDS,
            )
            if response.status_code < 400:
                break
            missing = _missing_column(response.text)
            if not missing or missing not in payload:
                break
            payload.pop(missing)
            dropped.append(missing)

        ok = response.status_code < 400
        status = {"configured": True, "ok": ok, "status_code": response.status_code}
        if dropped:
            status["dropped_columns"] = dropped
            sys.stdout.write(
                "GOLFBOT_RACE_SINK_DRIFT dropped unmapped column(s) "
                f"{', '.join(dropped)} -- the races table needs a migration; "
                "the rest of the record was saved\n"
            )
            sys.stdout.flush()
        if not ok:
            status["body"] = response.text[:200]
            sys.stdout.write(
                f"GOLFBOT_RACE_SINK_ERROR {response.status_code}: {response.text[:200]}\n"
            )
            sys.stdout.flush()
        return status
    except Exception as exc:  # noqa: BLE001 — telemetry must never kill a run
        try:
            sys.stdout.write(f"GOLFBOT_RACE_SINK_ERROR {type(exc).__name__}: {exc}\n")
            sys.stdout.flush()
        except Exception:
            pass
        return {"configured": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


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
    # Pre-staged (Future Sight) hold fires at T+0, in priority order. One entry
    # per target: {time, result, latency_ms, error}. result is hold_accepted /
    # rejected / error / skipped.
    prestage: list[dict[str, Any]] = field(default_factory=list)
    # Why Future Sight armed or stood down, keyed to this race. The reason used
    # to live only in the browser's event log, so a lost race left no record of
    # whether we pre-staged at all -- on 2026-08-31 the grid gate disarmed it
    # silently and three days of races looked identical in the database.
    prestage_decision: list[dict[str, Any]] = field(default_factory=list)
    # Things that compromised this race, or would have. These exist because
    # every failure this bot has had was visible in the data at the time and
    # nobody saw it: a workflow failing weekly into an empty room, a registry
    # timeout logged to stdout, a decision line confidently asserting something
    # false. A race that was doomed before it started should SAY SO, in the
    # summary, where a human already looks -- not wait to be excavated.
    warnings: list[dict[str, Any]] = field(default_factory=list)

    # 2FA / hold-keepalive evidence. Populated only when the booking-code path
    # fires. hold_refresh_failed distinguishes "the hold died while the user
    # fetched the code" from "the hold was fine and something else rejected us"
    # — two different bugs that look identical without it.
    two_factor_triggered: bool = False
    code_wait_ms: Optional[float] = None
    hold_refresh_ok: int = 0
    hold_refresh_failed: int = 0
    hold_refresh_first_error: str = ""

    # Self-collision: how many of our own jobs were racing these exact criteria
    # when this one armed, and which pick-order rotation it was given. Without
    # this, losing to one of our own users is indistinguishable in the history
    # from losing to a stranger's bot.
    peer_ordinal: int = 0
    peer_count: int = 1

    # Fee-required course: the slot was held and a payment link issued. Not a
    # win (no reservation exists yet) and not a loss (we beat everyone to it).
    awaiting_payment: bool = False

    captcha_solves: int = 0
    captcha_refreshes: int = 0
    captcha_stale_at_submit: bool = False

    # Did the container hit its CPU quota during the race? Windows measured from
    # a baseline taken before the release: "release" ends at T+5s (where races
    # are decided), "race" at the end. None where the counters are unreadable.
    # Lives inside timing (jsonb), so it needs no column.
    cpu_throttle: Optional[dict[str, Any]] = None

    booked: bool = False
    # None until a booking is attempted; False when ForeUp answered 200 without
    # anything identifying a reservation.
    booking_confirmed: Optional[bool] = None
    auto_book: bool = False
    aborted_reason: str = ""
    error: str = ""

    _started: float = field(default_factory=time.perf_counter)
    _release_monotonic: Optional[float] = None
    _cpu_baseline: Optional[dict[str, int]] = None
    _cpu_baseline_at: float = 0.0

    # ------------------------------------------------------------------
    # recording
    # ------------------------------------------------------------------
    def mark_release_instant(self, release_monotonic: float) -> None:
        """Anchor the release moment on the monotonic clock for offset math."""
        self._release_monotonic = release_monotonic

    def mark_cpu_baseline(self) -> None:
        """Snapshot the CPU-throttle counters. Call while idle, before T+0."""
        self._cpu_baseline = read_cpu_throttle()
        self._cpu_baseline_at = time.perf_counter()

    def record_cpu_window(self, name: str) -> None:
        """Record throttling since the baseline under `name`. Never raises."""
        try:
            delta = cpu_throttle_delta(self._cpu_baseline, read_cpu_throttle(),
                                       time.perf_counter() - self._cpu_baseline_at)
            if delta is not None:
                if self.cpu_throttle is None:
                    self.cpu_throttle = {}
                self.cpu_throttle[name] = delta
        except Exception:  # noqa: BLE001 -- measurement must never break a race
            pass

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

    def record_decision(self, stage: str, detail: str) -> None:
        """Bank an arming decision on the race row itself.

        Keyed to run_id, so 'why did we not pre-stage' is one SQL query rather
        than timestamp-matching against a log stream with finite retention.
        """
        self.prestage_decision.append(
            {"stage": stage, "detail": detail[:600], "at": _now_iso()}
        )

    def record_prestage(self, target_time: str, result: str, latency_ms: Optional[float] = None,
                        error: str = "", payload_sent: Any = None) -> None:
        entry: dict[str, Any] = {
            "time": target_time,
            "result": result,
            "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
            "error": error[:600],
            "at": _now_iso(),
        }
        # The burst builds its payload from a stored template rather than a
        # live slot, so when ForeUp refuses, what we SENT is the only evidence
        # of why. probe_cold_hold hands it back; keep it.
        trimmed = self._trim(payload_sent, self.PAYLOAD_FORENSIC_KEYS)
        if trimmed:
            entry["payload_sent"] = trimmed
        self.prestage.append(entry)

    # What a slot and a hold payload must agree about for ForeUp to accept.
    # Kept small on purpose: a full teesheet row is ~60 keys of noise, and the
    # point is a diff a human can read at a glance.
    SLOT_FORENSIC_KEYS = (
        "time", "available_spots", "available_spots_18", "booking_class_id",
        "teesheet_side_id", "schedule_id", "course_id", "green_fee",
        "minimum_players", "maximum_players_per_booking", "allowed_group_sizes",
        "booking_fee_required", "require_credit_card",
    )
    PAYLOAD_FORENSIC_KEYS = (
        "time", "players", "holes", "schedule_id", "course_id",
        "booking_class_id", "teesheet_side_id", "green_fee",
    )

    @staticmethod
    def _trim(src: Any, keys: tuple[str, ...]) -> dict[str, Any]:
        if not isinstance(src, dict):
            return {}
        return {k: src[k] for k in keys if k in src}

    def record_attempt(
        self,
        slot_time: str,
        result: str,
        error: str = "",
        latency_ms: Optional[float] = None,
        response: Any = None,
        slot_polled: Any = None,
        payload_sent: Any = None,
    ) -> None:
        """Record one booking attempt.

        `response` is what ForeUp actually sent back. It previously lived only
        in an in-memory event log that died with the container, so a booking
        that reported success but never appeared in the account left no evidence
        at all. It rides inside `attempts` (already jsonb) so no schema change
        is needed.
        """
        entry: dict[str, Any] = {
            "slot_time": slot_time,
            "result": result,
            "error": error[:300],
            "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
            "at": _now_iso(),
        }
        # On 2026-11-04 ForeUp refused three times its own availability API had
        # just listed as open, with the same six-word error it uses for times
        # that do not exist. With only that string there is nothing to reason
        # from. These two carry the slot AS ADVERTISED next to the payload we
        # actually sent, so the next refusal is a diff rather than a theory.
        slot_t = self._trim(
            (slot_polled or {}).get("raw") if isinstance(slot_polled, dict) and "raw" in slot_polled
            else slot_polled, self.SLOT_FORENSIC_KEYS)
        if isinstance(slot_polled, dict):
            for k in ("time", "available_spots", "green_fee"):
                if k in slot_polled and k not in slot_t:
                    slot_t[k] = slot_polled[k]
        if slot_t:
            entry["slot_polled"] = slot_t
        payload_t = self._trim(payload_sent, self.PAYLOAD_FORENSIC_KEYS)
        if payload_t:
            entry["payload_sent"] = payload_t
        if slot_t and payload_t:
            entry["mismatch"] = self._mismatch(slot_t, payload_t)
        if response is not None:
            entry["response"] = str(response)[:2000]
        self.attempts.append(entry)

    # ------------------------------------------------------------------
    # classification
    # ------------------------------------------------------------------
    # ForeUp's wording when a course requires a booking fee we do not pay. The
    # hold succeeds and only the conversion is refused, so without this the run
    # lands in lost_at_submit and reads as a race we were too slow for — which
    # sends every future investigation after latency instead of payment.
    FEE_MARKERS = ("booking fee is required", "booking fee required")

    def blocked_by_fee(self) -> bool:
        """Did every failed attempt fail because the course wants a fee?"""
        failed = [a for a in self.attempts if a.get("result") == "failed"]
        if not failed:
            return False
        return all(
            any(m in str(a.get("error", "")).lower() for m in self.FEE_MARKERS)
            for a in failed
        )

    # ForeUp distinguishes these, and the difference is the whole diagnosis.
    # "Time not available."            -> that time is not on the sheet at all
    # "...no longer available."        -> it was, and somebody else took it
    # Losing to a competitor is a race. Firing at times that do not exist is a
    # targeting failure, and no amount of speed fixes it.
    OFF_SHEET_MARKERS = ("time not available",)
    TAKEN_MARKERS = ("no longer available",)

    @staticmethod
    def _mismatch(slot: dict[str, Any], sent: dict[str, Any]) -> list[str]:
        """Name every way the payload disagrees with the slot as advertised."""
        out: list[str] = []

        def _s(v: Any) -> str:
            return str(v).strip() if v is not None else ""

        for key in ("booking_class_id", "teesheet_side_id", "schedule_id", "course_id"):
            if key in slot and key in sent and _s(slot[key]) != _s(sent[key]):
                out.append(f"{key}: slot={_s(slot[key])} sent={_s(sent[key])}")

        spots = slot.get("available_spots_18", slot.get("available_spots"))
        try:
            if spots is not None and sent.get("players") is not None \
                    and int(sent["players"]) > int(spots):
                out.append(f"players: asked {sent['players']} but slot had {spots} spot(s)")
        except (TypeError, ValueError):
            pass

        allowed = slot.get("allowed_group_sizes")
        if isinstance(allowed, list) and sent.get("players") is not None:
            if _s(sent["players"]) not in {_s(a) for a in allowed}:
                out.append(f"players: {sent['players']} not in allowed_group_sizes {allowed}")

        try:
            minimum = slot.get("minimum_players")
            if minimum is not None and sent.get("players") is not None \
                    and int(sent["players"]) < int(minimum):
                out.append(f"players: {sent['players']} below minimum_players {minimum}")
        except (TypeError, ValueError):
            pass
        return out

    def record_warning(self, code: str, detail: str) -> None:
        """Flag something that compromised this race. Safe to call repeatedly."""
        if any(w.get("code") == code for w in self.warnings):
            return
        self.warnings.append({"code": code, "detail": detail[:400], "at": _now_iso()})

    def _derive_warnings(self) -> None:
        """Warnings the data itself proves, computed at flush."""
        fires = self.prestage or []
        if fires and not any(p.get("result") == "hold_accepted" for p in fires):
            rejected = [p for p in fires if p.get("result") == "rejected"]
            off_sheet = [
                p for p in rejected
                if any(m in str(p.get("error", "")).lower() for m in self.OFF_SHEET_MARKERS)
            ]
            if rejected and len(off_sheet) == len(rejected):
                times = sorted({str(p.get("time")) for p in fires})
                self.record_warning(
                    "aimed_off_sheet",
                    f"All {len(fires)} pre-staged holds were refused as "
                    f"\"Time not available\" — not lost to a competitor, but fired at "
                    f"times this sheet does not offer ({', '.join(times)}). The grid or "
                    f"release floor we aimed with is wrong or stale for this date.",
                )

    def classify(self) -> str:
        if self.awaiting_payment:
            return OUTCOME_AWAITING_PAYMENT
        if self.booked:
            return OUTCOME_WON if self.booking_confirmed else OUTCOME_UNCONFIRMED
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
        if self.blocked_by_fee():
            return OUTCOME_BLOCKED_BY_FEE
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
        if self.prestage:
            accepted = sum(1 for p in self.prestage if p.get("result") == "hold_accepted")
            bits.append(f"pre-staged {len(self.prestage)} ({accepted} accepted)")
        if self.attempts:
            bits.append(f"{len(self.attempts)} submit attempts")
        if self.two_factor_triggered:
            bits.append(f"2FA code entered in {(self.code_wait_ms or 0) / 1000:.0f}s")
        if self.hold_refresh_failed:
            bits.append(
                f"HOLD REFRESH FAILED {self.hold_refresh_failed}/"
                f"{self.hold_refresh_failed + self.hold_refresh_ok}"
            )
        detail = " | ".join(bits)
        line = f"{outcome.upper()}: {reason}" + (f" [{detail}]" if detail else "")
        if self.warnings:
            # Front of the line, not a footnote: this is the part that explains
            # why an otherwise ordinary-looking loss was never winnable.
            flags = " ".join(f"!! {w['code'].upper()}" for w in self.warnings)
            first = self.warnings[0].get("detail", "")
            line = f"{flags} — {first} || {line}"
        return line

    # ------------------------------------------------------------------
    # output
    # ------------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        # Both fields below are measured against the release instant, which only
        # exists in timed sniper mode. Snipe-now sets the release to "now", so
        # the re-warm always "overruns" it and the offset is really just how long
        # arming took — recording those verbatim would make every snipe-now run
        # read as a late start in any later query, burying the timed races that
        # genuinely were. Absent beats misleading.
        timed = self.mode == "sniper"
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
            # Submitted-but-unverified must not count as a win, or any
            # success-rate query silently inflates.
            "success": bool(self.booked and self.booking_confirmed),
            "timing": {
                "rewarm_ms": self.rewarm_ms,
                "rewarm_overran_release": self.rewarm_overran_release if timed else None,
                "first_poll_offset_ms": self.first_poll_offset_ms if timed else None,
                "first_slot_seen_offset_ms": self.first_slot_seen_offset_ms,
                "detect_to_submit_ms": self.detect_to_submit_ms,
                "submit_to_confirm_ms": self.submit_to_confirm_ms,
                "total_race_ms": self.total_race_ms,
                "cpu_throttle": self.cpu_throttle,
            },
            "polls": self.polls.as_dict(),
            "attempts": self.attempts,
            "prestage": self.prestage,
            "prestage_decision": self.prestage_decision,
            "warnings": self.warnings,
            "two_factor": {
                "triggered": self.two_factor_triggered,
                "code_wait_ms": self.code_wait_ms,
                "hold_refresh_ok": self.hold_refresh_ok,
                "hold_refresh_failed": self.hold_refresh_failed,
                "hold_refresh_first_error": self.hold_refresh_first_error,
            },
            "peers": {
                "ordinal": self.peer_ordinal,
                "count": self.peer_count,
            },
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
        # Before the record is built, so the summary line carries them.
        try:
            self._derive_warnings()
        except Exception:  # noqa: BLE001 — a warning must never cost the record
            pass
        record = self.as_dict()

        line = json.dumps(record, default=str)

        # stdout first, and unconditionally: it costs nothing and it is the
        # fallback record if the durable sink below is unreachable.
        try:
            sys.stdout.write("GOLFBOT_RACE " + line + "\n")
            sys.stdout.flush()
        except Exception:
            pass

        # Append rather than one-file-per-run: a single JSONL file is what you
        # actually want when correlating many races.
        try:
            log_dir = Path(os.getenv("GOLFBOT_LOG_DIR", "logs"))
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "races.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

        record["sink"] = _post_to_sink(record)
        return record
