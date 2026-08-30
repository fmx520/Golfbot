"""Persistent per-course tee-grid registry.

A pre-staged hold has to name a time the course actually offers, and the only
way to know those times ahead of a release is to have learned the sheet from a
day that is already visible. Learning it fresh every arm has two failure modes:
the reference day can be booked solid (nothing to learn from), and the
knowledge evaporates the moment the job ends.

So every teesheet the bot reads upserts what it saw into the ``course_grids``
table, keyed by (course_id, day_of_week) — weekend and weekday sheets can
differ, so Saturdays teach us about Saturdays. The stored row carries the
interval, the phase (any observed time mod the interval — stable regardless of
which slots happened to be open), and the raw slot template a hold payload
needs. Arming then prefers a live read but falls back to the registry, so a
fully-booked reference day no longer means guessing.

Reuses the race-log sink's Supabase credentials: the grids endpoint is derived
from RACE_LOG_WEBHOOK_URL by swapping the table name. Everything is best effort
with a short timeout — this runs at arm time, never on the race path, and a
registry failure must never block a race.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Optional

from .race_log import _sink_headers

TIMEOUT_SECONDS = 5.0


def _grids_url() -> str:
    base = os.getenv("RACE_LOG_WEBHOOK_URL", "").strip()
    if not base:
        return ""
    url = re.sub(r"/races/?$", "/course_grids", base)
    return url if url != base else ""


def _log(detail: str) -> None:
    try:
        sys.stdout.write(f"GOLFBOT_GRID_STORE {detail}\n")
        sys.stdout.flush()
    except Exception:
        pass


def upsert_grid(
    *,
    course_id: str,
    day_of_week: int,
    grid: dict[str, Any],
    template: dict[str, Any],
    source_date: str,
) -> bool:
    """Store one grid observation. Never raises."""
    url = _grids_url()
    if not url or not grid.get("interval_minutes"):
        return False

    interval = int(grid["interval_minutes"])
    first = str(grid.get("first_time") or "")
    phase: Optional[int] = None
    if first and ":" in first:
        try:
            h, m = first.split(":")[:2]
            phase = (int(h) * 60 + int(m)) % interval
        except ValueError:
            phase = None

    row = {
        "course_id": str(course_id),
        "day_of_week": int(day_of_week),
        "interval_minutes": interval,
        "anchor_phase": phase,
        "first_time": first,
        "last_time": str(grid.get("last_time") or ""),
        "observed_count": int(grid.get("observed_count") or 0),
        "confidence": str(grid.get("confidence") or ""),
        "template": template or {},
        "source_date": source_date,
    }
    try:
        import httpx

        resp = httpx.post(
            f"{url}?on_conflict=course_id,day_of_week",
            json=row,
            headers={**_sink_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            _log(f"upsert failed {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — registry must never block a race
        _log(f"upsert error {type(exc).__name__}: {exc}")
        return False


def fetch_grid(*, course_id: str, day_of_week: int) -> Optional[dict[str, Any]]:
    """Read the stored grid for a course/day. Returns None when absent or on error."""
    url = _grids_url()
    if not url:
        return None
    try:
        import httpx

        resp = httpx.get(
            url,
            params={
                "course_id": f"eq.{course_id}",
                "day_of_week": f"eq.{int(day_of_week)}",
                "limit": "1",
            },
            headers=_sink_headers(),
            timeout=TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            _log(f"fetch failed {resp.status_code}: {resp.text[:200]}")
            return None
        rows = resp.json()
        if isinstance(rows, list) and rows:
            return rows[0]
        return None
    except Exception as exc:  # noqa: BLE001
        _log(f"fetch error {type(exc).__name__}: {exc}")
        return None


def matches_stored_grid(time_str: str, stored: dict[str, Any]) -> Optional[bool]:
    """Validate a target against a stored grid row. None when the row is unusable."""
    interval = stored.get("interval_minutes")
    phase = stored.get("anchor_phase")
    if not interval or phase is None:
        return None
    parts = str(time_str).strip().split(":")
    try:
        minutes = int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return None
    return minutes % int(interval) == int(phase)


def parse_hhmm_row(stored: dict[str, Any]) -> str:
    """Human-readable one-liner for events."""
    return (
        f"{stored.get('interval_minutes')}min grid, phase {stored.get('anchor_phase')}, "
        f"range {stored.get('first_time')}–{stored.get('last_time')}, "
        f"learned {stored.get('source_date')} ({stored.get('observed_count')} slots, "
        f"{stored.get('confidence')})"
    )
