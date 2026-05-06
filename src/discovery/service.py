from __future__ import annotations

from dataclasses import dataclass

from .interfaces import InventoryPoller, ScanCriteria, TeeTime, TeeTimeFilter


@dataclass(slots=True)
class DiscoveryService:
    poller: InventoryPoller
    tee_time_filter: TeeTimeFilter

    def scan(self, criteria: ScanCriteria) -> list[TeeTime]:
        return self.tee_time_filter.match(self.poller.poll(criteria), criteria)
