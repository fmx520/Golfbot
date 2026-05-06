from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from src.discovery.foreup_client import ForeUpError, ForeUpDiscoveryClient, RetryableHttpError, StaleSlotError

logger = logging.getLogger(__name__)


class DuplicateBookingAttemptError(ForeUpError):
    pass


@dataclass(frozen=True)
class BookingLatency:
    request_id: str
    total_ms: float
    stages: dict[str, float]


class ForeUpBookingClient(ForeUpDiscoveryClient):
    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._idempotency_lock = threading.Lock()
        self._inflight_keys: set[str] = set()
        self._completed_keys: set[str] = set()

    def book_slot(
        self,
        *,
        idempotency_key: str,
        slot_payload: dict[str, Any],
        confirm_payload: dict[str, Any],
        request_id: Optional[str] = None,
    ) -> tuple[dict[str, Any], BookingLatency]:
        req_id = request_id or str(uuid.uuid4())
        flow_started = time.perf_counter()
        stage_latencies: dict[str, float] = {}

        self._acquire_idempotency_key(idempotency_key)
        try:
            stage_latencies["slot_validation"] = self._timed_stage(
                "slot_validation",
                lambda: self._request("POST", "/api/slots/validate", stage="slot_validation", request_id=req_id, retry_safe=False, json=slot_payload),
            )
            stage_latencies["slot_hold"] = self._timed_stage(
                "slot_hold",
                lambda: self._request("POST", "/api/slots/hold", stage="slot_hold", request_id=req_id, retry_safe=False, json=slot_payload),
            )

            response: dict[str, Any] = {}
            stage_latencies["final_booking"] = self._timed_stage(
                "final_booking",
                lambda: response.update(
                    self._request(
                        "POST",
                        "/api/bookings/confirm",
                        stage="final_booking",
                        request_id=req_id,
                        retry_safe=False,
                        headers={"Idempotency-Key": idempotency_key},
                        json=confirm_payload,
                    ).json()
                ),
            )

            self._mark_idempotent_complete(idempotency_key)
            total_ms = round((time.perf_counter() - flow_started) * 1000, 2)
            self._log_flow(req_id, flow_started, total_ms, stage_latencies, "ok")
            return response, BookingLatency(request_id=req_id, total_ms=total_ms, stages=stage_latencies)
        except RetryableHttpError:
            self._release_idempotency_key(idempotency_key)
            self._log_flow(req_id, flow_started, None, stage_latencies, "error")
            raise
        except ForeUpError:
            self._release_idempotency_key(idempotency_key)
            self._log_flow(req_id, flow_started, None, stage_latencies, "error")
            raise

    def _timed_stage(self, stage_name: str, fn: Any) -> float:
        started = time.perf_counter()
        try:
            fn()
        except RetryableHttpError:
            raise
        except ForeUpError:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (409, 410, 422):
                raise StaleSlotError(f"stale-slot race detected at {stage_name}") from exc
            if status == 429 or 500 <= status <= 599:
                raise RetryableHttpError(f"transient error during {stage_name}: {status}") from exc
            raise ForeUpError(f"booking stage {stage_name} failed") from exc
        return round((time.perf_counter() - started) * 1000, 2)

    def _acquire_idempotency_key(self, key: str) -> None:
        with self._idempotency_lock:
            if key in self._completed_keys or key in self._inflight_keys:
                raise DuplicateBookingAttemptError(f"booking attempt blocked for key={key}")
            self._inflight_keys.add(key)

    def _mark_idempotent_complete(self, key: str) -> None:
        with self._idempotency_lock:
            self._inflight_keys.discard(key)
            self._completed_keys.add(key)

    def _release_idempotency_key(self, key: str) -> None:
        with self._idempotency_lock:
            self._inflight_keys.discard(key)

    def _log_flow(
        self,
        request_id: str,
        flow_started: float,
        total_ms: Optional[float],
        stages: dict[str, float],
        result: str,
    ) -> None:
        ended_at = datetime.now(timezone.utc).isoformat()
        total = total_ms if total_ms is not None else round((time.perf_counter() - flow_started) * 1000, 2)
        logger.info(
            "foreup_booking_flow",
            extra={
                "request_id": request_id,
                "ended_at": ended_at,
                "latency_ms": total,
                "stage_latencies_ms": stages,
                "result": result,
            },
        )
