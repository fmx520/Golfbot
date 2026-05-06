from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Callable, TypeVar

from .interfaces import MetricsSink, StructuredLogger

T = TypeVar("T")


@dataclass(slots=True)
class TelemetryService:
    metrics: MetricsSink
    logger: StructuredLogger

    def measure(self, operation: str, fn: Callable[[], T]) -> T:
        start = perf_counter()
        try:
            result = fn()
            self.metrics.increment("success_total", tags={"operation": operation})
            self.logger.info("operation_success", operation=operation)
            return result
        except Exception as exc:
            self.metrics.increment("failure_total", tags={"operation": operation})
            self.logger.error("operation_failure", operation=operation, error=str(exc))
            raise
        finally:
            latency_ms = (perf_counter() - start) * 1000
            self.metrics.observe_latency_ms(operation, latency_ms)
