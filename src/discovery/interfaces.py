from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from typing import Iterable, Protocol


@dataclass(slots=True)
class ScanCriteria:
    course_id: str
    play_date: date
    players: int
    time_start: time
    time_end: time


@dataclass(slots=True)
class TeeTime:
    course_id: str
    tee_time_id: str
    starts_at: str
    available_spots: int
    price_cents: int


class InventoryPoller(Protocol):
    def poll(self, criteria: ScanCriteria) -> Iterable[TeeTime]:
        ...


class TeeTimeFilter(Protocol):
    def match(self, tee_times: Iterable[TeeTime], criteria: ScanCriteria) -> list[TeeTime]:
        ...
