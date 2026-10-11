from __future__ import annotations

import logging
import threading
import re
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


# Keys ForeUp uses to acknowledge a reservation it actually created. Any one of
# these carrying a truthy value is our positive signal.
# Confirmed against a real Montauk booking, whose response carried both
# "TTID" and "teetime_id".
_CONFIRMATION_KEYS = (
    "reservation_id", "TTID", "ttid", "teetime_id", "confirmation_number",
    "reservation_number", "confirmation", "id",
)


def classify_reservation_response(response: dict[str, Any]) -> tuple[Optional[bool], str]:
    """Decide whether a 200 response actually created a reservation.

    ForeUp answers a rejected booking with HTTP 200 and a failure body, so
    `raise_for_status` alone is not evidence of a booking. Returns
    (confirmed, reason): True on a positive signal, False on an explicit
    failure, and None when the body is ambiguous.

    None is deliberately not treated as failure. We do not know every shape a
    successful response can take, and wrongly rejecting a real booking is worse
    than reporting one we cannot verify — so the caller flags it for a manual
    check rather than claiming either outcome.
    """
    if not isinstance(response, dict) or not response:
        return None, "empty response body"

    if response.get("success") is False:
        return False, f"ForeUp returned success=false: {str(response)[:200]}"

    for key in ("error", "errors"):
        val = response.get(key)
        if val:
            return False, f"ForeUp returned {key}: {str(val)[:200]}"

    for key in _CONFIRMATION_KEYS:
        if response.get(key):
            return True, f"{key}={response[key]}"

    if "raw_body" in response:
        return None, f"non-JSON response (HTTP {response.get('status_code')}): {str(response.get('raw_body'))[:200]}"

    return None, f"no reservation identifier in response: {str(response)[:200]}"


class ForeUpBookingClient(ForeUpDiscoveryClient):
    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._idempotency_lock = threading.Lock()
        self._inflight_keys: set[str] = set()
        self._completed_keys: set[str] = set()

    def refresh_pending_reservation(
        self,
        *,
        pending_reservation_id: str,
        request_id: Optional[str] = None,
    ) -> Optional[str]:
        """Keep a pending reservation hold alive. Browser polls this every ~10s.

        Returns None on success, or the error string on failure. Still best
        effort — the caller must not raise — but the outcome is no longer
        thrown away. A hold that dies mid-2FA looks exactly like a hold that
        survived, and that ambiguity cost us a diagnosis.

        Runs on a background thread during the code wait, never on the booking
        path, so returning a value costs nothing.
        """
        req_id = request_id or str(uuid.uuid4())
        path = f"/index.php/api/booking/refresh_pending_reservation/{pending_reservation_id}"
        try:
            self._request(
                "POST", path,
                stage="refresh_pending",
                request_id=req_id,
                retry_safe=False,
                content=b"",
            )
            return None
        except ForeUpError as exc:
            return str(exc)[:300]

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
        skip_validation: bool = False,
    ) -> tuple[dict[str, Any], BookingLatency]:
        req_id = request_id or str(uuid.uuid4())
        flow_started = time.perf_counter()
        stage_latencies: dict[str, float] = {}

        self._acquire_idempotency_key(idempotency_key)
        try:
            if not skip_validation:
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

            response = self._finish_hold(
                slot=slot, raw=raw,
                schedule_id=schedule_id, course_id=course_id or schedule_id,
                players=players, holes=holes,
                pending_reservation_id=pending_reservation_id,
                captcha_token=captcha_token,
                customer_id=customer_id,
                request_code_callback=request_code_callback,
                request_id=req_id, emit=_emit,
                stage_latencies=stage_latencies,
            )

            self._mark_idempotent_complete(idempotency_key)
            total_ms = round((time.perf_counter() - flow_started) * 1000, 2)
            self._log_flow(req_id, flow_started, total_ms, stage_latencies,
                           "awaiting_payment" if response.get("awaiting_payment") else "ok")
            return response, BookingLatency(request_id=req_id, total_ms=total_ms, stages=stage_latencies)
        except (RetryableHttpError, ForeUpError):
            self._release_idempotency_key(idempotency_key)
            self._log_flow(req_id, flow_started, None, stage_latencies, "error")
            raise

    def _finish_hold(
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
        customer_id: Optional[str],
        request_code_callback: Optional[Callable[[str], str]],
        request_id: str,
        emit: Callable[[str, dict[str, Any]], None],
        stage_latencies: Optional[dict[str, float]] = None,
    ) -> dict[str, Any]:
        """Turn a hold we already own into a booking -- or, on a course that
        charges a booking fee, into a payment link.

        There are two ways to win a hold: the polled path (`book_slot`) and a
        pre-staged fire (`complete_prestaged_hold`). This is the ONE place that
        finishes either. The fee recovery used to live inline in `book_slot`
        only, so on 2026-10-10 Future Sight won three Bethpage holds, met "A
        booking fee is required", had no recovery, and gave the race away. The
        fee path had only ever been exercised through polling.

        Returns the reservation response, or a dict with awaiting_payment=True
        and the hosted payment URL.
        """
        def _pick(key: str, default: Any = "") -> Any:
            val = raw.get(key, slot.get(key, default))
            return default if val is None else val

        common = dict(
            slot=slot, raw=raw, schedule_id=schedule_id, course_id=course_id,
            players=players, holes=holes,
            pending_reservation_id=pending_reservation_id,
            captcha_token=captcha_token, request_id=request_id, emit=emit,
        )

        # ---- Courses that charge a booking fee cannot be committed by us at
        # all. On those, users/reservations only validates -- a captured browser
        # checkout sends validate_only and gets {"valid":true} -- and the
        # reservation is created when the payment processor calls back. So take
        # the browser's route: get the code, validate with it, then ask for the
        # hosted payment page and hand its URL back. The hold stays ours until
        # it is paid. ----
        # A true flag lets us skip a commit that would only be refused. A false
        # one proves nothing -- see the recovery below.
        if _pick("booking_fee_required") and request_code_callback and customer_id:
            return self._hold_and_request_payment(
                **common, customer_id=customer_id,
                request_code_callback=request_code_callback,
            )

        # ---- Submit the reservation; if the course requires a booking code,
        # ForeUp returns a 400 with that exact message -- recover by calling
        # request_booking_code, waiting for the user to enter the emailed code,
        # and retrying the same submission with the code. ----
        response: dict[str, Any] = {}
        code_used: list[str] = []
        fee_blocked: list[str] = []

        def _do_submit() -> None:
            try:
                response.update(self._submit_with_2fa_recovery(
                    **common,
                    customer_id=customer_id,
                    request_code_callback=request_code_callback,
                    code_sink=code_used,
                ))
            except ForeUpError as exc:
                # The slot's booking_fee_required flag is not trustworthy --
                # Montauk reports false and still charges -- so the fee path
                # cannot be gated on it. ForeUp saying so IS the signal.
                # We already hold the slot, so recover into the payment flow
                # rather than losing a race we had won.
                if "booking fee" not in str(exc).lower():
                    raise
                fee_blocked.append(str(exc))

        submit_ms = self._timed_stage("reservation_submit", _do_submit)
        if stage_latencies is not None:
            stage_latencies["reservation_submit"] = submit_ms

        if not fee_blocked:
            return response

        if not (request_code_callback and customer_id):
            raise ForeUpError(
                f"This course charges a booking fee and there is no way to "
                f"collect it here: {fee_blocked[0]}"
            )
        emit("booking_fee_detected", {"error": fee_blocked[0]})
        return self._hold_and_request_payment(
            **common, customer_id=customer_id,
            request_code_callback=request_code_callback,
            existing_code=code_used[0] if code_used else "",
        )

    def _hold_and_request_payment(
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
        customer_id: str,
        request_code_callback: Callable[[str], str],
        request_id: str,
        emit: Callable[[str, dict[str, Any]], None],
        existing_code: str = "",
    ) -> dict[str, Any]:
        """Drive a fee-required booking as far as we are able: to the payment page.

        We already hold the slot. What we cannot do is create the reservation --
        that happens when the payment completes -- so this gets the emailed
        code, validates with it, and returns the hosted payment URL for a human
        to finish. Verified end to end against a live course.
        """
        def _pick(key: str, default: Any = "") -> Any:
            val = raw.get(key, slot.get(key, default))
            return default if val is None else val

        code = existing_code or ""
        if not code:
            self.request_booking_code(
                course_id=str(course_id),
                customer_id=str(customer_id),
                pending_reservation_id=pending_reservation_id,
                request_id=request_id,
            )
            emit("awaiting_booking_code", {"ttid": pending_reservation_id})
            code = request_code_callback(pending_reservation_id)
        if not code:
            raise ForeUpError("Booking code was not provided in time")

        validated = self.probe_validate_only(
            pending_reservation_id=pending_reservation_id,
            target_time=slot.get("raw_time", slot.get("time", "")),
            template=raw,
            schedule_id=str(schedule_id),
            course_id=str(course_id),
            players=players, holes=holes,
            captcha_token=captcha_token,
            confirmation_code=code,
            request_id=request_id,
        )
        if not validated.get("ok"):
            raise ForeUpError(
                f"ForeUp would not validate the booking: {validated.get('error') or validated}"
            )

        payment = self.request_booking_fee_payment(
            facility_id=str(course_id),
            teesheet_id=str(schedule_id),
            pending_reservation_id=pending_reservation_id,
            confirmation_code=code,
            target_time=slot.get("raw_time", slot.get("time", "")),
            players=players, holes=holes,
            booking_class_id=str(_pick("booking_class_id", "")),
            teesheet_side_id=str(_pick("teesheet_side_id", "")),
            available_spots=int(_pick("available_spots", players) or players),
            request_id=request_id,
        )
        emit("booking_fee_payment", {"payment": payment})
        if not payment.get("payment_url"):
            raise ForeUpError(
                f"Could not open a payment page for the held slot: "
                f"{payment.get('error') or payment.get('verdict')}"
            )

        fee = _pick("booking_fee_price", 0)
        per_person = bool(_pick("booking_fee_per_person", False))
        total = (fee * players) if per_person else fee
        return {
            "awaiting_payment": True,
            "pending_reservation_id": pending_reservation_id,
            "payment_url": payment["payment_url"],
            "booking_fee_total": total,
            "players": players,
            "slot_time": slot.get("time", ""),
        }

    def _submit_reservation(
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
        confirmation_code: Optional[str],
        request_id: str,
        emit: Callable[[str, dict[str, Any]], None],
    ) -> dict[str, Any]:
        """POST the full reservation for an existing hold. Shared by the polled
        booking path and the pre-staged path so 2FA and payload behaviour cannot
        drift between them."""
        full_payload = self._build_full_reservation_payload(
            slot=slot, raw=raw,
            schedule_id=schedule_id, course_id=course_id,
            players=players, holes=holes,
            pending_reservation_id=pending_reservation_id,
            captcha_token=captcha_token,
            validate_only=False,
            confirmation_code=confirmation_code,
        )
        logger.info("reservation_payload", extra={"payload": full_payload, "slot_raw": slot})
        emit("reservation_payload", {"payload": full_payload, "with_code": bool(confirmation_code)})
        try:
            resp = self._request(
                "POST",
                FOREUP_RESERVATIONS_PATH,
                stage="reservation_submit",
                request_id=request_id,
                retry_safe=False,
                json=full_payload,
            )
        except ForeUpError as exc:
            # emit() reaches the job's in-memory event log, which dies with the
            # container and only exists for sniper runs. The body of this error
            # is the single most diagnostic string we ever get -- it is what
            # separates "a booking fee is required" from "time not available"
            # from "a booking code is required" -- so it also goes to the log
            # stream, where it survives and covers every worker.
            logger.warning(
                "reservation_error",
                extra={"error": str(exc)[:1200], "request_id": request_id,
                       "with_code": bool(confirmation_code)},
            )
            emit("reservation_error", {"error": str(exc)})
            raise
        try:
            response = dict(resp.json())
        except Exception:
            response = {"raw_body": resp.text[:1000], "status_code": resp.status_code}
        logger.info(
            "reservation_response",
            extra={"response": response, "status": resp.status_code,
                   "request_id": request_id},
        )
        emit("reservation_response", {"response": response, "status": resp.status_code})
        return response

    def _submit_with_2fa_recovery(
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
        customer_id: Optional[str],
        request_code_callback: Optional[Callable[[str], str]],
        request_id: str,
        emit: Callable[[str, dict[str, Any]], None],
        code_sink: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        common = dict(
            slot=slot, raw=raw, schedule_id=schedule_id, course_id=course_id,
            players=players, holes=holes,
            pending_reservation_id=pending_reservation_id,
            captcha_token=captcha_token, request_id=request_id, emit=emit,
        )
        try:
            return self._submit_reservation(confirmation_code=None, **common)
        except ForeUpError as exc:
            msg = str(exc).lower()
            if "booking code" not in msg or request_code_callback is None or not customer_id:
                raise
            emit("booking_code_required_detected", {"ttid": pending_reservation_id})
            self.request_booking_code(
                course_id=str(course_id or schedule_id),
                customer_id=str(customer_id),
                pending_reservation_id=pending_reservation_id,
                request_id=request_id,
            )
            emit("awaiting_booking_code", {"ttid": pending_reservation_id})
            code = request_code_callback(pending_reservation_id)
            if not code:
                raise ForeUpError("Booking code was not provided in time")
            if code_sink is not None:
                code_sink.append(code)
            return self._submit_reservation(confirmation_code=code, **common)

    def complete_prestaged_hold(
        self,
        *,
        idempotency_key: str,
        pending_reservation_id: str,
        target_time: str,
        template: dict[str, Any],
        schedule_id: str,
        course_id: str,
        players: int,
        holes: int = 18,
        captcha_token: Optional[str] = None,
        customer_id: Optional[str] = None,
        request_code_callback: Optional[Callable[[str], str]] = None,
        request_id: Optional[str] = None,
        debug_callback: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ) -> dict[str, Any]:
        """Finish the booking for a hold created by a pre-staged fire.

        The hold already exists, so this runs only what follows it — through
        `_finish_hold`, the same path a polled booking takes, so 2FA recovery
        and booking-fee recovery behave identically on both. `template` carries
        the per-course fields (side id, fees, booking class) cached from a
        reference teesheet, standing in for the slot data a poll would supply.
        Returns either the reservation response or an awaiting_payment dict.
        """
        req_id = request_id or str(uuid.uuid4())

        def _emit(event: str, data: dict[str, Any]) -> None:
            if debug_callback:
                try:
                    debug_callback(event, data)
                except Exception:
                    pass

        synthetic_slot = {"time": target_time, "raw_time": target_time, "raw": template}
        self._acquire_idempotency_key(idempotency_key)
        try:
            response = self._finish_hold(
                slot=synthetic_slot, raw=template,
                schedule_id=schedule_id, course_id=course_id or schedule_id,
                players=players, holes=holes,
                pending_reservation_id=pending_reservation_id,
                captcha_token=captcha_token,
                customer_id=customer_id,
                request_code_callback=request_code_callback,
                request_id=req_id, emit=_emit,
            )
            self._mark_idempotent_complete(idempotency_key)
            return response
        except (RetryableHttpError, ForeUpError):
            self._release_idempotency_key(idempotency_key)
            raise

    def probe_cold_hold(
        self,
        *,
        target_time: str,
        schedule_id: str,
        course_id: str,
        players: int,
        holes: int = 18,
        booking_class_id: str = "",
        teesheet_side_id: str = "",
        green_fee: Any = 0,
        request_id: Optional[str] = None,
        stage: str = "probe_cold_hold",
    ) -> dict[str, Any]:
        """Try to hold a tee time WITHOUT a preceding availability poll.

        This is the experiment behind pre-staging. Today the bot must poll, see
        a slot, then hold it — and that poll round trip (104-514ms observed) is
        the entire margin we lose by: ForeUp has told us a slot was gone 154ms
        after we saw it. If a hold can be fired from configuration alone, that
        round trip leaves the critical path.

        This ONLY creates the hold. It never calls the reservations endpoint, so
        it cannot book anything. A successful hold is real, though — the time is
        locked to this account until it expires (~2 min), so probe a slot you do
        not mind briefly holding.

        Returns a verdict dict rather than raising: a rejection is the useful
        result here, because ForeUp's 400s name the field they wanted.
        """
        req_id = request_id or str(uuid.uuid4())
        payload = {
            "time": target_time,
            "holes": holes,
            "players": players,
            "carts": "false",
            "schedule_id": int(schedule_id),
            "teesheet_side_id": teesheet_side_id,
            "course_id": int(course_id or schedule_id),
            "booking_class_id": booking_class_id,
            "duration": 1,
            "foreup_discount": "false",
            "foreup_trade_discount_rate": 0,
            "trade_min_players": 0,
            "cart_fee": 0,
            "cart_fee_tax": 0,
            "green_fee": green_fee,
            "green_fee_tax": 0,
        }

        started = time.perf_counter()
        try:
            resp = self._request(
                "POST",
                FOREUP_PENDING_RESERVATION_PATH,
                stage=stage,
                request_id=req_id,
                retry_safe=False,
                data=payload,
            )
        except ForeUpError as exc:
            return {
                "accepted": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "error": str(exc)[:1000],
                "payload_sent": payload,
                "verdict": "REJECTED — see error for the field ForeUp wanted.",
            }

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            body = resp.json()
        except Exception:
            body = {"raw_body": resp.text[:1000], "status_code": resp.status_code}

        held_id = body.get("reservation_id", "") if isinstance(body, dict) else ""
        return {
            "accepted": bool(held_id),
            "latency_ms": latency_ms,
            "reservation_id": held_id,
            "response": body,
            "payload_sent": payload,
            "verdict": (
                f"ACCEPTED in {latency_ms:.0f}ms — pre-staging works. This hold is "
                f"REAL and expires on its own in ~2 min; nothing was booked."
                if held_id else
                "Returned 200 but no reservation_id — inconclusive, read the response."
            ),
        }

    def release_pending_reservation(
        self,
        *,
        pending_reservation_id: str,
        request_id: Optional[str] = None,
    ) -> bool:
        """Hand a hold back to the sheet instead of squatting on it.

        Firing every pre-staged target at once means we can win several holds
        and only want one. A hold expires on its own in ~2 minutes, but leaving
        three of them parked is exactly the behaviour that gets an account
        flagged, and it takes those times off the sheet for everyone else --
        including the other five people using this bot. This is the DELETE the
        browser itself sends when you back out of a booking.

        Best effort by design: a failed release is not worth failing a booking
        over, and the hold expires regardless.
        """
        if not pending_reservation_id:
            return False
        try:
            self._request(
                "DELETE",
                f"{FOREUP_PENDING_RESERVATION_PATH}/{pending_reservation_id}",
                stage="release_pending_reservation",
                request_id=request_id or str(uuid.uuid4()),
                retry_safe=False,
            )
            return True
        except Exception:  # noqa: BLE001 -- see docstring; the hold self-expires
            return False

    def request_booking_fee_payment(
        self,
        *,
        facility_id: str,
        teesheet_id: str,
        pending_reservation_id: str,
        confirmation_code: str,
        target_time: str,
        players: int,
        holes: int,
        booking_class_id: str,
        teesheet_side_id: str,
        available_spots: int = 1,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Ask ForeUp for the hosted payment page that settles the booking fee.

        This is the step our booking path has never taken. On a fee-required
        course, `users/reservations` does not create anything -- it answers
        {"valid":true} and stops. The reservation is created server-side when
        the payment processor calls back, so this request, and the hosted form
        it returns, IS the booking.

        Form-encoded, not JSON: the field list and encoding are taken verbatim
        from a captured browser checkout. Returns the hosted payment URL, which
        carries an Elavon TransactionSetupID and no ForeUp session -- the whole
        question being whether that URL can be opened somewhere other than the
        browser that created it.

        Charges nothing by itself. It only sets up a payment page.
        """
        req_id = request_id or str(uuid.uuid4())
        payload = {
            "players": players,
            "available_spots": available_spots,
            "carts": "false",
            "time": target_time,
            "holes": holes,
            "schedule_id": teesheet_id,
            "course_id": facility_id,
            "special_id": "false",
            "recipient": "false",
            "booking_class_id": booking_class_id,
            "promo_code": "",
            "player_list": "false",
            "duration": 1,
            "pending_reservation_id": pending_reservation_id,
            "customer_message": "",
            "frontend_course_id": facility_id,
            "reservation_confirmation_uid": confirmation_code,
            "teesheet_side_id": teesheet_side_id,
        }

        started = time.perf_counter()
        try:
            resp = self._request(
                "POST",
                f"/index.php/booking/credit_card_window/{facility_id}/purchase-fee",
                stage="booking_fee_window",
                request_id=req_id,
                retry_safe=False,
                data=payload,
            )
        except ForeUpError as exc:
            return {
                "ok": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "error": str(exc)[:1200],
                "payload_sent": payload,
                "verdict": "ForeUp refused to open a payment window — read the error.",
            }

        html = resp.text or ""
        match = re.search(r"""src=["'](https://[^"']*hostedpayments[^"']*)["']""", html)
        setup = re.search(r"TransactionSetupID=([A-Za-z0-9-]+)", html)
        return {
            "ok": bool(match),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "payment_url": match.group(1) if match else "",
            "transaction_setup_id": setup.group(1) if setup else "",
            "payload_sent": payload,
            "raw_html": html[:1200],
            "verdict": (
                "Payment page created. Open this URL on another device: if the card "
                "form loads there, the hand-off works and paying it should create the "
                "reservation. If it errors, the session cannot leave this browser."
                if match else
                "No hosted payment URL in the response — read raw_html. Either the "
                "hold or the code was not accepted, or this course settles the fee "
                "some other way."
            ),
        }

    def reservation_payload_keys(self) -> list[str]:
        """Every field we send on a reservation, for diffing against ForeUp's own.

        Built from the real builder rather than a hand-kept list, so it cannot
        drift away from what actually goes over the wire.
        """
        sample = self._build_full_reservation_payload(
            slot={"time": "", "raw": {}}, raw={}, schedule_id="0", course_id="0",
            players=1, holes=18, pending_reservation_id="", captcha_token=None,
            validate_only=True, confirmation_code=None,
        )
        return sorted(sample.keys())

    def probe_validate_only(
        self,
        *,
        pending_reservation_id: str,
        target_time: str,
        template: dict[str, Any],
        schedule_id: str,
        course_id: str,
        players: int,
        holes: int = 18,
        captcha_token: Optional[str] = None,
        confirmation_code: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Ask ForeUp whether it WOULD accept this booking, without booking it.

        `validate_only` is ForeUp's own field — it exists in our payload only
        because it exists in the requests their site makes — and we have always
        hardcoded it False. Sending True puts a question to their server that we
        have so far only been able to reason about from the outside: does the
        booking-fee check run here, or at commit?

        That matters because it decides a whole branch of work. The failing
        bookings return "A booking fee is required to book this tee time." at
        the reservations endpoint. If that same message comes back under
        validate_only, the check is part of validation and no amount of
        reordering our requests to match the browser's will get past it — we
        would need to actually pay. If validation passes, the answer is
        genuinely unknown rather than encouraging: it may only be checking the
        shape of the request.

        Never raises: a rejection is the informative outcome, and its body is
        the whole point. The caller must have created the hold first.

        CAUTION: this trusts ForeUp to honour their own flag. We have never
        exercised it. If their backend ignores validate_only, this books for
        real — so aim it at a slot you would be happy to have.
        """
        req_id = request_id or str(uuid.uuid4())
        slot = {"time": target_time, "raw_time": target_time, "raw": template}
        payload = self._build_full_reservation_payload(
            slot=slot,
            raw=template,
            schedule_id=schedule_id,
            course_id=course_id,
            players=players,
            holes=holes,
            pending_reservation_id=pending_reservation_id,
            captcha_token=captcha_token,
            validate_only=True,
            confirmation_code=confirmation_code,
        )

        started = time.perf_counter()
        try:
            resp = self._request(
                "POST",
                FOREUP_RESERVATIONS_PATH,
                stage="probe_validate_only",
                request_id=req_id,
                retry_safe=False,
                json=payload,
            )
        except ForeUpError as exc:
            err = str(exc)
            low = err.lower()
            fee = "booking fee" in low
            # The 2FA gate sits ahead of the fee gate in the real checkout, so
            # hitting it means we have not reached the question we came to ask
            # — not that the fee is fine. Reporting it as "some other error"
            # would read as progress when it is nothing of the kind.
            needs_code = "booking code" in low or "confirmation" in low
            if needs_code:
                verdict = ("Stopped at the 2FA gate, BEFORE the fee is even considered. "
                           "This says nothing about the fee yet — enter the emailed code "
                           "to push validation past this point.")
            elif fee:
                verdict = ("FEE CHECK RUNS AT VALIDATION. Matching the browser's request "
                           "order will not help — the fee has to actually be paid.")
            else:
                verdict = ("Rejected for something other than the fee or the code — read "
                           "the error; it names the field ForeUp wanted.")
            return {
                "ok": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "fee_blocked": fee,
                "needs_code": needs_code,
                "code_sent": bool(confirmation_code),
                "error": err[:1500],
                "validate_only_sent": True,
                "verdict": verdict,
            }

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            body = resp.json()
        except Exception:
            body = {"raw_body": resp.text[:1500], "status_code": resp.status_code}

        return {
            "ok": True,
            "latency_ms": latency_ms,
            "fee_blocked": False,
            "needs_code": False,
            "code_sent": bool(confirmation_code),
            "response": body,
            "validate_only_sent": True,
            "verdict": (
                f"Validation passed in {latency_ms:.0f}ms. This is the normal step, "
                f"not an ambiguous one: a captured browser checkout sends exactly this "
                f"and gets exactly {{\"valid\":true}} back. Nothing is booked yet — on a "
                f"course that charges a booking fee, the reservation is created when "
                f"the payment completes."
            ),
        }

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
