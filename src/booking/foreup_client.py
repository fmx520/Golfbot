from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from src.discovery.foreup_client import (
    FOREUP_API_KEY,
    FOREUP_RESERVATIONS_PATH,
    FOREUP_TIMES_PATH,
    AntiBotDetectedError,
    ForeUpDiscoveryClient,
    ForeUpError,
    RetryableHttpError,
    StaleSlotError,
    iso_to_foreup_date,
)

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
        slot: dict[str, Any],
        schedule_id: str,
        date: str,
        players: int,
        holes: int = 18,
        request_id: Optional[str] = None,
        captcha_token: Optional[str] = None,
    ) -> tuple[dict[str, Any], BookingLatency]:
        req_id = request_id or str(uuid.uuid4())
        flow_started = time.perf_counter()
        stage_latencies: dict[str, float] = {}

        self._acquire_idempotency_key(idempotency_key)
        try:
            stage_latencies["slot_validation"] = self._timed_stage(
                "slot_validation",
                lambda: self._validate_slot_still_available(
                    schedule_id=schedule_id,
                    date=date,
                    slot=slot,
                    request_id=req_id,
                ),
            )

            reservation_payload = {
                "time": slot.get("raw_time", slot.get("time", "")),
                "date": iso_to_foreup_date(date),
                "holes": str(holes),
                "players": str(players),
                "schedule_id": str(schedule_id),
                "course_id": str(slot.get("schedule_id", schedule_id)),
                "booking_class_id": str(slot.get("booking_class_id", "")),
                "teesheet_id": str(slot.get("teesheet_id", "")),
                "green_fee": str(slot.get("green_fee", "")),
                "api_key": FOREUP_API_KEY,
            }
            if captcha_token:
                reservation_payload["g-recaptcha-response"] = captcha_token

            logger.info("reservation_payload", extra={"payload": reservation_payload, "slot_raw": slot})

            def _submit_reservation() -> None:
                resp = self._request(
                    "POST",
                    FOREUP_RESERVATIONS_PATH,
                    stage="reservation_submit",
                    request_id=req_id,
                    retry_safe=False,
                    data=reservation_payload,
                )
                try:
                    response.update(resp.json())
                except Exception:
                    response.update({"raw_body": resp.text[:1000], "status_code": resp.status_code})

            response: dict[str, Any] = {}
            stage_latencies["reservation_submit"] = self._timed_stage(
                "reservation_submit",
                _submit_reservation,
            )

            self._mark_idempotent_complete(idempotency_key)
            total_ms = round((time.perf_counter() - flow_started) * 1000, 2)
            self._log_flow(req_id, flow_started, total_ms, stage_latencies, "ok")
            return response, BookingLatency(request_id=req_id, total_ms=total_ms, stages=stage_latencies)
        except (RetryableHttpError, ForeUpError):
            self._release_idempotency_key(idempotency_key)
            self._log_flow(req_id, flow_started, None, stage_latencies, "error")
            raise

    def _validate_slot_still_available(
        self,
        *,
        schedule_id: str,
        date: str,
        slot: dict[str, Any],
        request_id: str,
    ) -> None:
        params: dict[str, Any] = {
            "time": "all",
            "schedule_id": schedule_id,
            "date": iso_to_foreup_date(date),
            "api_key": FOREUP_API_KEY,
        }
        if slot.get("booking_class_id"):
            params["booking_class"] = slot["booking_class_id"]
        response = self._request(
            "GET",
            FOREUP_TIMES_PATH,
            stage="slot_validation",
            request_id=request_id,
            params=params,
            retry_safe=True,
        )
        try:
            raw = response.json()
        except Exception:
            raise ForeUpError("Could not parse availability response during validation")

        items = raw if isinstance(raw, list) else raw.get("times", raw.get("slots", []))
        if not isinstance(items, list):
            raise ForeUpError("Unexpected availability response format")

        target_time = slot.get("time", "")
        target_id = slot.get("tee_time_id", "")
        for item in items:
            item_time = str(item.get("time", item.get("starts_at", "")))
            item_id = str(item.get("tee_time_id", item.get("teeTimeId", item.get("id", ""))))
            available = int(item.get("available_spots", item.get("spots", item.get("openSpots", 0))))
            if (item_id and item_id == target_id) or target_time in item_time:
                if available >= 1:
                    return
                raise StaleSlotError(f"Slot {target_time} no longer available (0 spots)")
        raise StaleSlotError(f"Slot {target_time} not found in current availability")

    def _timed_stage(self, stage_name: str, fn: Any) -> float:
        started = time.perf_counter()
        try:
            fn()
        except (RetryableHttpError, AntiBotDetectedError):
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
