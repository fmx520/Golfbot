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

import os
import re
import sys
import time
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


# A grid is only worth what it can speak for. The booking API returns ONLY open
# slots, so a nearly-booked day yields a thin, perfectly self-consistent sample
# -- and a blind upsert lets that sample overwrite a grid learned from a freshly
# released sheet. That is not hypothetical: a 9-minute twilight sample once
# replaced what should have been a 10-minute morning grid, and the pre-staged
# holds fired at 07:06 and 07:15, times that do not exist on that sheet, for
# twelve straight 400s.
#
# Span first (36 times across eight hours describes the sheet; 8 across eighty
# minutes describes one corner of it), then observation count.
GRID_STALE_DAYS = 45


def _hhmm_minutes(value: str) -> Optional[int]:
    try:
        hh, mm = str(value).split(":")[:2]
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def _grid_quality(first: str, last: str, observed: int) -> tuple[int, int]:
    lo, hi = _hhmm_minutes(first), _hhmm_minutes(last)
    span = (hi - lo) if (lo is not None and hi is not None and hi >= lo) else 0
    return (span, int(observed or 0))


def _is_today(source_date: str) -> bool:
    """Is this reading a sheet for today, which shrinks as the day passes?"""
    try:
        from datetime import date as _date
        y, m, d = (int(x) for x in str(source_date).split("-")[:3])
        return _date(y, m, d) == _date.today()
    except (ValueError, TypeError):
        return False


def _row_is_stale(row: dict[str, Any]) -> bool:
    """Grids drift with the season, so a very old row should not win forever."""
    stamp = str(row.get("updated_at") or row.get("source_date") or "")[:10]
    if not stamp:
        return False
    try:
        from datetime import date as _date
        y, m, d = (int(x) for x in stamp.split("-")[:3])
        return (_date.today() - _date(y, m, d)).days > GRID_STALE_DAYS
    except (ValueError, TypeError):
        return False


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

    last = str(grid.get("last_time") or "")
    observed = int(grid.get("observed_count") or 0)

    # Do not let a thinner read clobber a better one. The write is an upsert on
    # (course_id, day_of_week) with merge-duplicates, i.e. a blind overwrite, so
    # this comparison is the only thing standing between a fully-booked
    # reference day and the grid Future Sight aims with.
    existing, read_ok = fetch_grid_result(course_id=str(course_id), day_of_week=int(day_of_week))
    if not read_ok:
        # Could not see what is stored, so cannot tell whether this write would
        # be a downgrade. Skip it: a missed refresh costs nothing, a thin read
        # overwriting a release-day grid costs a race.
        _log(f"skipped upsert for course {course_id} dow {day_of_week}: "
             f"could not read the stored row to compare against")
        return False
    if existing:
        new_q = _grid_quality(first, last, observed)
        old_q = _grid_quality(
            str(existing.get("first_time") or ""),
            str(existing.get("last_time") or ""),
            existing.get("observed_count") or 0,
        )
        same_date = str(existing.get("source_date") or "") == str(source_date)
        # Re-reading the SAME date is normally a refresh rather than a
        # downgrade -- prices and side ids move, and the newer read is truer.
        #
        # Except when that date is today. Today's sheet only ever shrinks: a
        # tee time that has passed stops being returned, because the booking
        # API lists only OPEN slots. Re-reading it at 10am after a 7am read
        # does not produce a truer grid, it produces a later, narrower one.
        # Observed on 2026-09-01: course 2432 Tuesday went 16:42->17:09 and
        # 7->6 slots across a single morning, purely from time passing.
        refresh_ok = same_date and not (_is_today(source_date) and new_q < old_q)
        if not refresh_ok and new_q < old_q and not _row_is_stale(existing):
            _log(
                f"kept existing grid for course {course_id} dow {day_of_week}: "
                f"stored {old_q[0]}min span / {old_q[1]} slots "
                f"({existing.get('first_time')}-{existing.get('last_time')}, "
                f"from {existing.get('source_date')}) beats new "
                f"{new_q[0]}min span / {new_q[1]} slots ({first}-{last}, "
                f"from {source_date}) -- not overwriting"
            )
            return True

    row = {
        "course_id": str(course_id),
        "day_of_week": int(day_of_week),
        "interval_minutes": interval,
        "anchor_phase": phase,
        "first_time": first,
        "last_time": last,
        "observed_count": observed,
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


# Reading the grid runs at ARM time, minutes before a release, never on the
# race path -- so a transient failure has budget to be retried, and must be.
FETCH_ATTEMPTS = 3
FETCH_RETRY_DELAY_S = 0.6


def fetch_grid_result(
    *, course_id: str, day_of_week: int
) -> tuple[Optional[dict[str, Any]], bool]:
    """Read the stored grid. Returns (row_or_None, ok).

    ``ok`` is False ONLY when the read itself failed. A successful read of a
    course/day we have never seen returns (None, True).

    That distinction is the whole point of this function. On 2026-11-04 a
    single 5s ReadTimeout here made the Wednesday grid for Bethpage Black
    invisible; the caller read None as "never seen", inferred the rhythm from
    other weekdays, and borrowed a release floor from a Monday read taken two
    months earlier (08:30). The real Wednesday sheet started at 09:30, so all
    24 pre-staged holds came back "Time not available." The row was there the
    entire time -- we just could not see it for five seconds, and never asked
    again.
    """
    url = _grids_url()
    if not url:
        return None, False

    last_detail = ""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
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
                last_detail = f"{resp.status_code}: {resp.text[:200]}"
                _log(f"fetch failed (attempt {attempt}/{FETCH_ATTEMPTS}) {last_detail}")
            else:
                rows = resp.json()
                if isinstance(rows, list) and rows:
                    return rows[0], True
                return None, True
        except Exception as exc:  # noqa: BLE001 -- registry must never block a race
            last_detail = f"{type(exc).__name__}: {exc}"
            _log(f"fetch error (attempt {attempt}/{FETCH_ATTEMPTS}) {last_detail}")

        if attempt < FETCH_ATTEMPTS:
            time.sleep(FETCH_RETRY_DELAY_S)

    _log(
        f"fetch gave up for course {course_id} dow {day_of_week} after "
        f"{FETCH_ATTEMPTS} attempts ({last_detail}) -- caller must NOT read this "
        f"as 'no grid stored'"
    )
    return None, False


def fetch_grid(*, course_id: str, day_of_week: int) -> Optional[dict[str, Any]]:
    """Read the stored grid for a course/day. None when absent OR on error.

    Prefer fetch_grid_result() where the difference matters.
    """
    row, _ok = fetch_grid_result(course_id=course_id, day_of_week=day_of_week)
    return row


# A tee sheet's interval and phase belong to the COURSE, not the day. Across
# every course we have read on more than one day, all days agree:
#
#   2431 Black    Mon, Sun                    10min @ :00
#   2432 Red      Mon Tue Wed Thu Fri Sun      9min @ :03
#   2433 Blue     Wed Thu Fri Sat              9min @ :03
#   2434 Green    Tue Wed Fri Sat              9min @ :03
#   2436 Montauk  6 days                       9min @ :03
#   2437          Tue Wed Thu Fri Sat          9min @ :06
#
# Zero counterexamples in 27 rows. Note 2437 runs :06 where its neighbours run
# :03 -- phase is course-specific, so this infers from the SAME course's other
# days and never globally.
#
# What does not transfer is first_time/last_time: the operating window really
# does differ (weekend early starts), and green fees vary by day. That is fine,
# because _gen_targets() builds targets inside the user's requested window and
# never consults first/last. Interval plus phase is all it needs.
#
# Without this, learning Saturday means waiting for a Saturday. With it, one
# read of any day makes every day aimable.
INFER_MIN_SIBLING_DAYS = 2
INFER_MIN_TOTAL_OBSERVED = 12


def fetch_course_grids(*, course_id: str) -> list[dict[str, Any]]:
    """Every stored day for one course. Returns [] when absent or on error."""
    url = _grids_url()
    if not url:
        return []
    try:
        import httpx

        resp = httpx.get(
            url,
            params={"course_id": f"eq.{course_id}", "limit": "7"},
            headers=_sink_headers(),
            timeout=TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            _log(f"fetch_course failed {resp.status_code}: {resp.text[:200]}")
            return []
        rows = resp.json()
        return rows if isinstance(rows, list) else []
    except Exception as exc:  # noqa: BLE001
        _log(f"fetch_course error {type(exc).__name__}: {exc}")
        return []


def infer_grid(*, course_id: str, day_of_week: int) -> Optional[dict[str, Any]]:
    """Derive a day's grid from the same course's other days.

    Returns a row shaped like fetch_grid's, plus ``inferred_from`` (the
    weekdays it was derived from) and ``observed_count`` totalling the
    observations backing it. Returns None unless every sibling day agrees on
    (interval, phase) -- a course that genuinely runs two different intervals
    must not be papered over.

    The template comes from the strongest sibling of the SAME day type
    (weekend vs weekday) where one exists, because green fees differ between
    them; failing that, the strongest sibling of any day.
    """
    siblings = [r for r in fetch_course_grids(course_id=str(course_id))
                if int(r.get("day_of_week", -1)) != int(day_of_week)
                and r.get("interval_minutes") and r.get("anchor_phase") is not None]
    if len(siblings) < INFER_MIN_SIBLING_DAYS:
        return None

    intervals = {int(r["interval_minutes"]) for r in siblings}
    phases = {int(r["anchor_phase"]) for r in siblings}
    if len(intervals) != 1 or len(phases) != 1:
        _log(
            f"cannot infer course {course_id} dow {day_of_week}: siblings "
            f"disagree (intervals {sorted(intervals)}, phases {sorted(phases)})"
        )
        return None

    total_seen = sum(int(r.get("observed_count") or 0) for r in siblings)
    if total_seen < INFER_MIN_TOTAL_OBSERVED:
        return None

    want_weekend = int(day_of_week) >= 5
    same_type = [r for r in siblings if (int(r["day_of_week"]) >= 5) == want_weekend]
    pool = same_type or siblings
    best = max(pool, key=lambda r: int(r.get("observed_count") or 0))

    return {
        "course_id": str(course_id),
        "day_of_week": int(day_of_week),
        "interval_minutes": intervals.pop(),
        "anchor_phase": phases.pop(),
        # Deliberately blank: we know the rhythm, not the operating window.
        "first_time": "",
        "last_time": "",
        "observed_count": total_seen,
        "confidence": "inferred",
        "template": best.get("template") or {},
        "source_date": str(best.get("source_date") or ""),
        "inferred_from": sorted(int(r["day_of_week"]) for r in siblings),
        "inferred_template_dow": int(best["day_of_week"]),
    }


# Where Future Sight should start aiming. The rhythm (interval + phase) belongs
# to the course, but the first time that is actually bookable online does not
# show up in it: on 2026-10-17 Future Sight fired 48 holds at Red 07:06-07:51
# and Black 07:00-07:50, every one refused, while the fullest sheet seen at that
# release started at 08:18 (Red) and 09:00 (Black) -- and on 10-10 Red's 08:00
# and 08:09 were refused while 08:27-08:45 were won. Times earlier than that are
# either not bookable online for the class or gone before any hold can land;
# either way a shot there wins nothing.
#
# Only a sheet read AT a release says where bookable times start: a harvest or
# reference read days later has had its mornings booked away. Release reads are
# recognised by date -- the race banks the play date's sheet the evening it
# opens, a week ahead -- and need enough times seen to describe the sheet: a
# 4-time read once recorded Black's Sunday as starting at 16:20, which as a
# floor would quietly push every target out of a morning window.
RELEASE_READ_MIN_DAYS_AHEAD = 7
RELEASE_FLOOR_MIN_SEEN = 20


def release_floor(row: Optional[dict[str, Any]]) -> Optional[str]:
    """Earliest time seen open on a release-day sheet, as 'HH:MM', or None."""
    if not row:
        return None
    first = str(row.get("first_time") or "")
    if _hhmm_minutes(first) is None:
        return None
    if int(row.get("observed_count") or 0) < RELEASE_FLOOR_MIN_SEEN:
        return None
    try:
        from datetime import date as _date, datetime as _dt
        from zoneinfo import ZoneInfo
        y, m, d = (int(x) for x in str(row.get("source_date") or "").split("-")[:3])
        played = _date(y, m, d)
        stamp = str(row.get("updated_at") or "").replace(" ", "T").replace("Z", "+00:00")
        # Postgres renders "+00"; fromisoformat wants "+00:00".
        if re.search(r"[+-]\d{2}$", stamp):
            stamp += ":00"
        # The release is an evening in New York; reading the date in UTC would
        # roll an 8pm EST release into the next day and miss it.
        read_on = _dt.fromisoformat(stamp).astimezone(ZoneInfo("America/New_York")).date()
    except (ValueError, TypeError):
        return None
    if (played - read_on).days < RELEASE_READ_MIN_DAYS_AHEAD:
        return None
    return first


def sibling_release_floor(*, course_id: str, day_of_week: int,
                          rows: Optional[list[dict[str, Any]]] = None) -> Optional[dict[str, Any]]:
    """Borrow a release floor from the same course's other days of the same type.

    10-25, Black Sunday: the only Sunday row was a 4-time read, release_floor
    rightly refused it, so there was no floor -- Future Sight fired 30 holds at
    08:00-08:50, all refused, while Black's Saturday release opened at 09:00 and
    polling's first sight in that window was 09:30. Where a bookable sheet
    starts is set by the course and differs between weekends and weekdays
    (early-weekend tee times, resident hours), so only days of the same type
    count. When several qualify the EARLIEST wins: it never skips a time some
    day has shown bookable, and polling covers the window regardless.

    Returns {"floor", "day_of_week", "source_date"} or None. `rows` is for
    tests; otherwise the course's stored rows are fetched.
    """
    if rows is None:
        rows = fetch_course_grids(course_id=str(course_id))
    want_weekend = int(day_of_week) >= 5
    best: Optional[dict[str, Any]] = None
    for row in rows or []:
        try:
            dow = int(row.get("day_of_week", -1))
        except (TypeError, ValueError):
            continue
        if dow == int(day_of_week) or (dow >= 5) != want_weekend:
            continue
        floor = release_floor(row)
        if not floor:
            continue
        if best is None or (_hhmm_minutes(floor) or 0) < (_hhmm_minutes(best["floor"]) or 0):
            best = {"floor": floor, "day_of_week": dow,
                    "source_date": str(row.get("source_date") or "")}
    return best


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
