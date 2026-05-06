from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import time

from .interfaces import PollingScheduler, TriggerWindow


@dataclass(slots=True)
class HighFrequencyScheduler(PollingScheduler):
    def run(self, interval: timedelta, trigger_window: TriggerWindow, task) -> None:
        while True:
            now = datetime.utcnow()
            if trigger_window.starts_at <= now <= trigger_window.ends_at:
                task()
            elif now > trigger_window.ends_at:
                break
            time.sleep(max(interval.total_seconds(), 0.01))
