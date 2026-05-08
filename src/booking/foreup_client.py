from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from src.discovery.foreup_client import (
    FOREUP_API_KEY,
    FOREUP_PENDING_RESERVATION_PATH,
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

    def request_booking_code(
        self,
        *,
        course_id: str,
        customer_id: str,
        pending_reservation_id: str,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Trigger ForeUp's email-based 2FA. Empty-body POST."""
        req_id = request_id or str(uuid.uuid4())
        path = f"/api_rest/index.php/courses/{course_id}/teetime/confirmation/{pending_reservation_id}/{customer_id}"
        resp = self._request(
            "POST",
            path,
            stage="request_booking_code",
            request_id=req_id,
            retry_safe=False,
            content=b"",
        )
        try:
            return resp.json()
        except Exception:
            return {"raw_body": resp.text[:500], "status_code": resp.status_code}

    def book_slot(
        self,
        *,
        idempotency_key: str,
        slot: dict[str, Any],
        schedule_id: str,
        course_id: str = "",
        date: str,
        players: int,
        holes: int = 18,
        request_id: Optional[str] = None,
        captcha_token: Optional[str] = None,
        debug_callback: Optional[Callable[[str, dict[str, Any]], None]] = None,
        customer_id: Optional[str] = None,
        request_code_callback: Optional[Callable[[str], str]] = None,
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

            raw = slot.get("raw") if isinstance(slot.get("raw"), dict) else slot

            def _pick(key: str, default: Any = "") -> Any:
                val = raw.get(key, slot.get(key, default))
                return default if val is None else val

            def _emit(event: str, data: dict[str, Any]) -> None:
                if debug_callback:
                    try:
                        debug_callback(event, data)
                    except Exception:
                        pass

            # ---- Step 1: pending_reservation (form-encoded) -> returns TTID ----
            pending_payload = {
                "time": slot.get("raw_time", slot.get("time", "")),
                "holes": holes,
                "players": players,
                "carts": "false",
                "schedule_id": int(schedule_id),
                "teesheet_side_id": _pick("teesheet_side_id"),
                "course_id": int(course_id or schedule_id),
                "booking_class_id": _pick("booking_class_id"),
                "duration": 1,
                "foreup_discount": "false",
                "foreup_trade_discount_rate": 0,
                "trade_min_players": _pick("trade_min_players", 0),
                "cart_fee": _pick("cart_fee", 0),
                "cart_fee_tax": _pick("cart_fee_tax", 0),
                "green_fee": _pick("green_fee", 0),
                "green_fee_tax": _pick("green_fee_tax", 0),
            }
            _emit("pending_payload", {"payload": pending_payload})

            pending_response: dict[str, Any] = {}

            def _create_pending() -> None:
                try:
                    resp = self._request(
                        "POST",
                        FOREUP_PENDING_RESERVATION_PATH,
                        stage="pending_reservation",
                        request_id=req_id,
                        retry_safe=False,
                        data=pending_payload,
                    )
                except ForeUpError as exc:
                    _emit("pending_error", {"error": str(exc)})
                    raise
                try:
                    pending_response.update(resp.json())
                except Exception:
                    pending_response.update({"raw_body": resp.text[:1000], "status_code": resp.status_code})
                _emit("pending_response", {"response": pending_response})

            stage_latencies["pending_reservation"] = self._timed_stage(
                "pending_reservation", _create_pending
            )

            pending_reservation_id = pending_response.get("reservation_id", "")
            if not pending_reservation_id:
                raise ForeUpError(
                    f"pending_reservation did not return a reservation_id: {pending_response}"
                )

            # ---- Step 1.5 (optional): request booking code, wait for user ----
            confirmation_code: Optional[str] = None
            if request_code_callback is not None:
                if not customer_id:
                    raise ForeUpError("request_code_callback set but customer_id missing")
                _emit("request_booking_code", {
                    "course_id": course_id, "customer_id": customer_id,
                    "ttid": pending_reservation_id,
                })
                self.request_booking_code(
                    course_id=str(course_id or schedule_id),
                    customer_id=str(customer_id),
                    pending_reservation_id=pending_reservation_id,
                    request_id=req_id,
                )
                _emit("awaiting_booking_code", {"ttid": pending_reservation_id})
                confirmation_code = request_code_callback(pending_reservation_id)
                if not confirmation_code:
                    raise ForeUpError("Booking code was not provided in time")

            # ---- Step 2: build the full reservation payload (JSON) ----
            full_payload = self._build_full_reservation_payload(
                slot=slot,
                raw=raw,
                schedule_id=schedule_id,
                course_id=course_id or schedule_id,
                players=players,
                holes=holes,
                pending_reservation_id=pending_reservation_id,
                captcha_token=captcha_token,
                validate_only=False,
                confirmation_code=confirmation_code,
            )
            logger.info("reservation_payload", extra={"payload": full_payload, "slot_raw": slot})
            _emit("reservation_payload", {"payload": full_payload})

            response: dict[str, Any] = {}

            def _submit_reservation() -> None:
                try:
                    resp = self._request(
                        "POST",
                        FOREUP_RESERVATIONS_PATH,
                        stage="reservation_submit",
                        request_id=req_id,
                        retry_safe=False,
                        json=full_payload,
                    )
                except ForeUpError as exc:
                    _emit("reservation_error", {"error": str(exc)})
                    raise
                try:
                    response.update(resp.json())
                except Exception:
                    response.update({"raw_body": resp.text[:1000], "status_code": resp.status_code})
                _emit("reservation_response", {"response": response, "status": resp.status_code})

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

    def submit_booking_code(
        self,
        *,
        code: str,
        reservation_id: str = "",
        schedule_id: str = "",
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        req_id = request_id or str(uuid.uuid4())
        payload: dict[str, Any] = {
            "booking_code": code,
            "api_key": FOREUP_API_KEY,
        }
        if reservation_id:
            payload["reservation_id"] = reservation_id
        if schedule_id:
            payload["schedule_id"] = schedule_id

        resp = self._request(
            "POST",
            f"{FOREUP_RESERVATIONS_PATH}/verify",
            stage="verify_code",
            request_id=req_id,
            retry_safe=False,
            json=payload,
        )
        try:
            return resp.json()
        except Exception:
            return {"raw_body": resp.text[:1000], "status_code": resp.status_code}

    def _build_full_reservation_payload(
        self,
        *,
        slot: dict[str, Any],
        raw: dict[str, Any],
        schedule_id: str,
        course_id: str,
        players: int,
        holes: int,
        pending_reservation_id: str,
        captcha_token: Optional[str],
        validate_only: bool,
        confirmation_code: Optional[str] = None,
    ) -> dict[str, Any]:
        def _pick(key: str, default: Any = "") -> Any:
            val = raw.get(key, slot.get(key, default))
            return default if val is None else val

        green_fee = _pick("green_fee", 0)
        green_fee_18 = _pick("green_fee_18", green_fee)
        guest_green_fee = _pick("guest_green_fee", green_fee)
        subtotal = green_fee
        time_str = slot.get("raw_time", slot.get("time", ""))

        air_quotes_cart = []
        if green_fee:
            air_quotes_cart.append({
                "type": "item",
                "description": "Green Fee",
                "price": green_fee,
                "quantity": int(players),
                "subtotal": green_fee * int(players),
            })

        return {
            "validate_only": validate_only,
            "time": time_str,
            "start_front": _pick("start_front"),
            "course_id": int(course_id),
            "course_name": _pick("course_name", ""),
            "schedule_id": int(schedule_id),
            "schedule_name": _pick("schedule_name", ""),
            "teesheet_id": _pick("teesheet_id", schedule_id),
            "teesheet_holes": _pick("teesheet_holes", holes),
            "teesheet_side_id": _pick("teesheet_side_id"),
            "teesheet_side_name": _pick("teesheet_side_name", ""),
            "teesheet_side_order": _pick("teesheet_side_order", 1),
            "reround_teesheet_side_id": _pick("reround_teesheet_side_id"),
            "reround_teesheet_side_name": _pick("reround_teesheet_side_name", ""),
            "booking_class_id": _pick("booking_class_id"),
            "pending_reservation_id": pending_reservation_id,
            "captchaid": captcha_token or "",
            "reservation_confirmation_uid": confirmation_code or "",
            "holes": str(holes),
            "players": str(players),
            "carts": False,
            "duration": 1,
            "rate_type": _pick("rate_type", "walking"),
            "reservation_type": "tee",
            "pay_online": _pick("pay_online", "no"),
            "pay_players": str(players),
            "pay_carts": 0,
            "pay_subtotal": subtotal,
            "pay_total": subtotal,
            "subtotal": subtotal,
            "preTaxSubtotal": subtotal,
            "total": subtotal,
            "totalWithTax": subtotal,
            "estimatedTax": 0,
            "discount": 0,
            "discount_percent": 0,
            "promo_code": "",
            "promo_discount": 0,
            "customer_message": "",
            "details": "",
            "end_datetime": "",
            "notes": [],
            "player_list": False,
            "purchased": False,
            "paid_player_count": 0,
            "reservation_confirmation_uid": "",
            "showTaxDetails": False,
            "display_capacity": _pick("display_capacity", False),
            "max_capacity": 0,
            "minimum_players": _pick("minimum_players", 1),
            "maximum_players_per_booking": _pick("maximum_players_per_booking", 4),
            "allowed_group_sizes": _pick("allowed_group_sizes", ["1", "2", "3", "4"]),
            "available_spots": _pick("available_spots", 1),
            "available_spots_9": _pick("available_spots_9", 0),
            "available_spots_18": _pick("available_spots_18", 1),
            "available_duration": None,
            "availableHoles": str(_pick("teesheet_holes", holes)),
            "increment_amount": None,
            "blockReservationDueToExistingReservation": False,
            "allow_mobile_checkin": 0,
            "airQuotesCart": air_quotes_cart,
            "green_fee": green_fee,
            "green_fee_9": _pick("green_fee_9", 0),
            "green_fee_18": green_fee_18,
            "green_fee_tax": _pick("green_fee_tax", 0),
            "green_fee_tax_9": _pick("green_fee_tax_9", 0),
            "green_fee_tax_18": _pick("green_fee_tax_18", 0),
            "green_fee_tax_rate": _pick("green_fee_tax_rate", False),
            "guest_green_fee": guest_green_fee,
            "guest_green_fee_9": _pick("guest_green_fee_9", 0),
            "guest_green_fee_18": _pick("guest_green_fee_18", guest_green_fee),
            "guest_green_fee_tax": _pick("guest_green_fee_tax", 0),
            "guest_green_fee_tax_9": _pick("guest_green_fee_tax_9", 0),
            "guest_green_fee_tax_18": _pick("guest_green_fee_tax_18", 0),
            "guest_green_fee_tax_rate": _pick("guest_green_fee_tax_rate", False),
            "cart_fee": _pick("cart_fee", 0),
            "cart_fee_9": _pick("cart_fee_9", 0),
            "cart_fee_18": _pick("cart_fee_18", 0),
            "cart_fee_tax": _pick("cart_fee_tax", 0),
            "cart_fee_tax_9": _pick("cart_fee_tax_9", 0),
            "cart_fee_tax_18": _pick("cart_fee_tax_18", 0),
            "cart_fee_tax_rate": _pick("cart_fee_tax_rate", False),
            "guest_cart_fee": _pick("guest_cart_fee", 0),
            "guest_cart_fee_9": _pick("guest_cart_fee_9", 0),
            "guest_cart_fee_18": _pick("guest_cart_fee_18", 0),
            "guest_cart_fee_tax": _pick("guest_cart_fee_tax", 0),
            "guest_cart_fee_tax_9": _pick("guest_cart_fee_tax_9", 0),
            "guest_cart_fee_tax_18": _pick("guest_cart_fee_tax_18", 0),
            "guest_cart_fee_tax_rate": _pick("guest_cart_fee_tax_rate", False),
            "booking_fee_required": _pick("booking_fee_required", False),
            "booking_fee_price": _pick("booking_fee_price", False),
            "booking_fee_per_person": _pick("booking_fee_per_person", False),
            "has_special": _pick("has_special", False),
            "special_id": _pick("special_id", False),
            "special_was_price": _pick("special_was_price", None),
            "special_discount_percentage": _pick("special_discount_percentage", 0),
            "group_id": _pick("group_id", False),
            "foreup_discount": _pick("foreup_discount", False),
            "foreup_trade_discount_rate": _pick("foreup_trade_discount_rate", 0),
            "foreup_trade_discount_information": [],
            "foreup_trade_refund_amount": 0,
            "is_designated_trade": _pick("is_designated_trade", False),
            "is_foreup_trade_reservation": False,
            "trade_available_players": _pick("trade_available_players", 0),
            "trade_cart_requirement": _pick("trade_cart_requirement", "both"),
            "trade_hole_requirement": _pick("trade_hole_requirement", "all"),
            "trade_min_players": _pick("trade_min_players", 0),
            "require_credit_card": _pick("require_credit_card", False),
        }

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
