"""High-speed booking executor.

This module implements a booking execution flow that prioritizes:
- low-latency discovery-to-submit behavior,
- pre-warmed session state,
- parallelized independent prechecks,
- fail-closed confirmation handling to prevent double-bookings.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple


class AmbiguousConfirmationError(RuntimeError):
    """Raised when a submit result cannot be confirmed safely."""


class BookingGateway(Protocol):
    """Protocol for external booking API integration."""

    def warm_session(self) -> None:
        """Warm upstream auth/session state before the release window."""

    def load_player_profile(self) -> Dict[str, Any]:
        """Fetch player profile and ensure account is ready."""

    def verify_payment_prerequisites(self) -> Dict[str, Any]:
        """Validate payment setup and anti-fraud prerequisites."""

    def read_cart_state(self) -> Dict[str, Any]:
        """Inspect cart to ensure no stale or conflicting pending state."""

    def discover_slots(self, course_filter: Any, date_filter: Any) -> Sequence[Dict[str, Any]]:
        """Return currently available slot candidates."""

    def submit_booking(self, slot: Dict[str, Any]) -> Dict[str, Any]:
        """Submit booking and return provider response."""


@dataclass
class PercentileTimer:
    """Thread-safe accumulator for latency metrics."""

    _samples: List[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, value_seconds: float) -> None:
        with self._lock:
            self._samples.append(value_seconds)

    def summary(self) -> Dict[str, Optional[float]]:
        with self._lock:
            samples = list(self._samples)

        if not samples:
            return {"count": 0, "p50_ms": None, "p95_ms": None}

        ordered = sorted(samples)
        return {
            "count": len(ordered),
            "p50_ms": _percentile_ms(ordered, 50),
            "p95_ms": _percentile_ms(ordered, 95),
        }


def _percentile_ms(sorted_seconds: Sequence[float], percentile: float) -> float:
    if not sorted_seconds:
        raise ValueError("cannot compute percentile of empty set")

    # Inclusive linear interpolation over [0..n-1].
    n = len(sorted_seconds)
    if n == 1:
        return sorted_seconds[0] * 1000.0

    position = (n - 1) * (percentile / 100.0)
    lower = int(position)
    upper = min(lower + 1, n - 1)
    fraction = position - lower
    value = sorted_seconds[lower] + (sorted_seconds[upper] - sorted_seconds[lower]) * fraction
    return value * 1000.0


@dataclass
class ExecutorMetrics:
    discovery_loop_interval: PercentileTimer = field(default_factory=PercentileTimer)
    detect_to_submit_latency: PercentileTimer = field(default_factory=PercentileTimer)
    submit_to_confirm_latency: PercentileTimer = field(default_factory=PercentileTimer)

    def snapshot(self) -> Dict[str, Dict[str, Optional[float]]]:
        return {
            "discovery_loop_interval": self.discovery_loop_interval.summary(),
            "detect_to_submit_latency": self.detect_to_submit_latency.summary(),
            "submit_to_confirm_latency": self.submit_to_confirm_latency.summary(),
        }


@dataclass
class BookingExecutor:
    gateway: BookingGateway
    is_target_slot: Callable[[Dict[str, Any]], bool]
    confirm_submission: Callable[[Dict[str, Any]], Optional[bool]]
    logger: Any
    metrics: ExecutorMetrics = field(default_factory=ExecutorMetrics)
    _filter_cache: Dict[Tuple[str, Any], Any] = field(default_factory=dict)

    def prewarm_before_release(self, release_epoch_seconds: float, prewarm_lead_seconds: float = 5.0) -> None:
        """Pre-warm session just before release to reduce first-request latency."""
        warm_at = release_epoch_seconds - prewarm_lead_seconds
        delay = warm_at - time.time()
        if delay > 0:
            time.sleep(delay)

        self.gateway.warm_session()
        self.logger.info("session prewarmed", extra={"release": release_epoch_seconds, "lead": prewarm_lead_seconds})

    def run_parallel_prechecks(self) -> Dict[str, Dict[str, Any]]:
        """Run independent prechecks concurrently."""
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                "player_profile": pool.submit(self.gateway.load_player_profile),
                "payment_prerequisites": pool.submit(self.gateway.verify_payment_prerequisites),
                "cart_state": pool.submit(self.gateway.read_cart_state),
            }
            results = {name: future.result() for name, future in futures.items()}

        self.logger.info("prechecks complete", extra={"checks": list(results.keys())})
        return results

    def _cached_filter(self, key: str, factory: Callable[[], Any]) -> Any:
        cache_key = (key, id(factory))
        if cache_key not in self._filter_cache:
            self._filter_cache[cache_key] = factory()
        return self._filter_cache[cache_key]

    def _discover(self, course_filter_factory: Callable[[], Any], date_filter_factory: Callable[[], Any]) -> Sequence[Dict[str, Any]]:
        course_filter = self._cached_filter("course", course_filter_factory)
        date_filter = self._cached_filter("date", date_filter_factory)
        return self.gateway.discover_slots(course_filter=course_filter, date_filter=date_filter)

    def wait_and_book(
        self,
        course_filter_factory: Callable[[], Any],
        date_filter_factory: Callable[[], Any],
        poll_interval_seconds: float = 0.05,
        timeout_seconds: float = 30.0,
    ) -> Dict[str, Any]:
        """Run discovery loop and submit immediately on first target slot match."""
        start = time.monotonic()
        loop_started_at = None

        while time.monotonic() - start <= timeout_seconds:
            now = time.monotonic()
            if loop_started_at is not None:
                self.metrics.discovery_loop_interval.observe(now - loop_started_at)
            loop_started_at = now

            slots = self._discover(course_filter_factory, date_filter_factory)
            detect_time = time.monotonic()

            for slot in slots:
                if not self.is_target_slot(slot):
                    continue

                submit_start = time.monotonic()
                self.metrics.detect_to_submit_latency.observe(submit_start - detect_time)

                submit_response = self.gateway.submit_booking(slot)
                confirm_start = time.monotonic()
                confirmed = self.confirm_submission(submit_response)
                confirm_end = time.monotonic()
                self.metrics.submit_to_confirm_latency.observe(confirm_end - confirm_start)

                if confirmed is not True:
                    # Fail closed: no retries/re-submits when confirmation is missing/uncertain.
                    raise AmbiguousConfirmationError(
                        "Booking confirmation was ambiguous; stopped to avoid possible double-booking."
                    )

                # Non-critical logging intentionally after submission/confirmation path.
                self.logger.info(
                    "booking confirmed",
                    extra={
                        "slot": slot,
                        "submit_response": submit_response,
                        "metrics": self.metrics.snapshot(),
                    },
                )
                return submit_response

            time.sleep(poll_interval_seconds)

        raise TimeoutError("No target slot matched before timeout")
