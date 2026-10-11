"""Infer a course's tee-time grid from an observed teesheet.

A pre-staged hold has to name an exact time. Courses run on a fixed grid — say
every 10 minutes from 06:30 — so "08:05" may simply not exist, and a hold fired
at it fails no matter how fast we are. Before we can pre-stage anything we have
to know which times a course actually offers.

The complication: the booking API only returns times with open spots, so the
sheet we see is the true grid minus whatever is already booked. Consecutive
gaps are therefore multiples of the real interval, not the interval itself.
We take the smallest observed gap as the candidate and then check every other
gap is a whole multiple of it, which is what separates a confident reading from
a guess.

Everything here is pure — no network — so it can be tested against real
teesheets captured once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import gcd
from typing import Any, Iterable, Optional


# Golf tee intervals run roughly 6-15 minutes. An inferred interval below this
# means the observed gaps share no real common grid, not that the course runs a
# very fast sheet.
MIN_PLAUSIBLE_INTERVAL_MIN = 5

# A common divisor this small is only believed when two open times are actually
# that far apart. Otherwise it is usually two real intervals meeting -- a
# 10-minute day running into a 15-minute twilight has GCD 5, and calling that a
# consistent 5-minute grid would aim half of Future Sight's holds at times that
# do not exist. Real 9/10-minute sheets clear this bar on the GCD alone, even
# when only every second or third slot is open (gaps 18 and 27 give 9).
MIN_UNOBSERVED_INTERVAL_MIN = 7


def _to_minutes(value: str) -> Optional[int]:
    """Parse 'HH:MM', 'YYYY-MM-DD HH:MM' or 'HH:MM:SS' into minutes past midnight."""
    if not value:
        return None
    text = str(value).strip()
    if " " in text:  # strip a leading date
        text = text.split(" ", 1)[1]
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes


def _to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class TeeGrid:
    """A course's inferred tee-time grid."""

    interval_minutes: Optional[int] = None
    first_time: str = ""
    last_time: str = ""
    observed_count: int = 0
    # Every time the grid implies between first and last, booked ones included.
    grid_times: list[str] = field(default_factory=list)
    # True when every observed time lands on the inferred grid. False means the
    # sheet is irregular (split intervals, shotgun starts) and the grid should
    # not be trusted for pre-staging.
    consistent: bool = False
    confidence: str = "none"  # none | low | medium | high
    notes: list[str] = field(default_factory=list)

    def is_valid_target(self, time_str: str) -> bool:
        minutes = _to_minutes(time_str)
        return minutes is not None and _to_hhmm(minutes) in set(self.grid_times)

    def matches_interval(self, time_str: str) -> bool:
        """Is this time on the course's interval, extrapolating beyond what we saw?

        `is_valid_target` only accepts times between the first and last slot we
        observed — but on a busy reference day the morning may be fully booked,
        so the observed span starts at noon while the release-day target is
        07:00. The interval and phase are properties of the sheet, not of what
        happens to be open, so validate against those: same modulus, same
        offset, any hour.
        """
        minutes = _to_minutes(time_str)
        if minutes is None or not self.interval_minutes or not self.first_time:
            return False
        anchor = _to_minutes(self.first_time)
        if anchor is None:
            return False
        return (minutes - anchor) % self.interval_minutes == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "interval_minutes": self.interval_minutes,
            "first_time": self.first_time,
            "last_time": self.last_time,
            "observed_count": self.observed_count,
            "grid_count": len(self.grid_times),
            "grid_times": self.grid_times,
            "consistent": self.consistent,
            "confidence": self.confidence,
            "notes": self.notes,
        }


def analyze_teesheet_grid(times: Iterable[str]) -> TeeGrid:
    """Infer the tee interval from the times on a teesheet.

    `times` may be raw slot times in any of the formats the discovery client
    emits. Booked slots are expected to be missing; that is handled.
    """
    minutes = sorted({m for m in (_to_minutes(t) for t in times) if m is not None})
    grid = TeeGrid(observed_count=len(minutes))

    if len(minutes) < 2:
        grid.notes.append(
            "Need at least two times to infer an interval; "
            f"got {len(minutes)}. Try a date with more availability."
        )
        return grid

    grid.first_time = _to_hhmm(minutes[0])
    grid.last_time = _to_hhmm(minutes[-1])

    deltas = [b - a for a, b in zip(minutes, minutes[1:]) if b > a]
    if not deltas:
        grid.notes.append("All observed times were identical.")
        return grid

    # Smallest gap is the best candidate for the true interval: any larger gap
    # is a run of booked slots. The GCD guards the case where every observed gap
    # is a multiple (e.g. we only ever saw every other slot), which would
    # otherwise inflate the interval.
    #
    # But the GCD collapses toward 1 on a sheet that is not on a uniform grid at
    # all, and every gap is trivially a multiple of 1 — which would report an
    # irregular sheet as perfectly consistent. Golf intervals are 6-15 minutes;
    # anything under MIN_PLAUSIBLE means the gaps share no real common grid.
    smallest = min(deltas)
    common = 0
    for d in deltas:
        common = gcd(common, d)

    if common >= MIN_PLAUSIBLE_INTERVAL_MIN and (
        common >= MIN_UNOBSERVED_INTERVAL_MIN or common in deltas
    ):
        interval = common
        shares_common_grid = True
    elif common >= MIN_PLAUSIBLE_INTERVAL_MIN:
        interval = common
        shares_common_grid = False
        grid.notes.append(
            f"Gaps ({sorted(set(deltas))}min) only share a {common}min divisor, and no two "
            f"open times are actually {common}min apart — this looks like two different "
            f"intervals meeting, not a {common}-minute sheet."
        )
    else:
        interval = smallest
        shares_common_grid = False
        grid.notes.append(
            f"Gaps ({sorted(set(deltas))}min) share no common interval of at least "
            f"{MIN_PLAUSIBLE_INTERVAL_MIN}min — this sheet is not on a uniform grid."
        )

    grid.interval_minutes = interval

    off_grid = [d for d in deltas if d % interval != 0]
    grid.consistent = shares_common_grid and not off_grid

    span = minutes[-1] - minutes[0]
    grid.grid_times = [_to_hhmm(minutes[0] + i * interval) for i in range(span // interval + 1)]

    observed_set = {_to_hhmm(m) for m in minutes}
    coverage = len(observed_set & set(grid.grid_times)) / max(1, len(observed_set))

    if not grid.consistent:
        grid.confidence = "low"
        grid.notes.append(
            f"{len(off_grid)} gap(s) are not a multiple of {interval}min — the sheet "
            "may use split intervals. Do not pre-stage against this grid."
        )
    elif len(minutes) >= 12 and coverage == 1.0:
        grid.confidence = "high"
    elif len(minutes) >= 5:
        grid.confidence = "medium"
        grid.notes.append(
            f"Only {len(minutes)} times observed. The interval is probably right, "
            "but the first/last tee times shift seasonally with daylight."
        )
    else:
        grid.confidence = "low"
        grid.notes.append(
            f"Only {len(minutes)} times observed — too few to be sure the smallest "
            "gap is the real interval rather than two booked slots apart."
        )

    return grid
