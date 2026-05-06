from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol


@dataclass(slots=True)
class TriggerWindow:
    starts_at: datetime
    ends_at: datetime


class PollingScheduler(Protocol):
    def run(self, interval: timedelta, trigger_window: TriggerWindow, task: Callable[[], None]) -> None:
        ...
