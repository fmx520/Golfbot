from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class MetricPoint:
    name: str
    value: float
    tags: dict[str, str]


class MetricsSink(Protocol):
    def observe_latency_ms(self, operation: str, latency_ms: float, tags: dict[str, str] | None = None) -> None:
        ...

    def increment(self, counter: str, amount: int = 1, tags: dict[str, str] | None = None) -> None:
        ...


class StructuredLogger(Protocol):
    def info(self, message: str, **fields: object) -> None:
        ...

    def error(self, message: str, **fields: object) -> None:
        ...
