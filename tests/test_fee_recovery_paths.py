"""Booking-fee recovery must behave the same whichever way a hold was won.

There are two ways to win a hold: the polled path (`book_slot`) and a
pre-staged fire (`complete_prestaged_hold`). The fee recovery was added to the
first only, so on 2026-10-10 Future Sight won three Bethpage holds, ForeUp
answered "A booking fee is required to book this tee time.", and the
pre-staged path had nothing to do with that but give the race away.

These run the real ForeUpBookingClient against a fake ForeUp (no network) and
require both paths to come back with a payment link, whether the slot's
booking_fee_required flag is true (Bethpage) or false (the flag cannot be
trusted, which is why the error text is the real signal).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.booking.foreup_client import ForeUpBookingClient  # noqa: E402
from src.discovery.foreup_client import ForeUpError  # noqa: E402

FEE_REFUSAL = ("Client error '400 Bad Request' for url "
               "'https://foreupsoftware.com/index.php/api/booking/users/reservations' | "
               'Body: {"success":false,"msg":"A booking fee is required to book this tee time."}')
TAKEN = ("Client error '400 Bad Request' | "
         'Body: {"success":false,"msg":"Time not available."}')


class _Resp:
    status_code = 200
    text = '<iframe src="https://hostedpayments.example/pay?TransactionSetupID=abc-123">'

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeForeUp(ForeUpBookingClient):
    """ForeUp as it answered on 2026-10-10, plus the payment hand-off."""

    def __init__(self, submit_error: str = FEE_REFUSAL):
        super().__init__("https://example.invalid")
        self.calls: list[str] = []
        self.submit_error = submit_error

    def _request(self, method, path, *, stage, **kw):
        self.calls.append(stage)
        if stage == "pending_reservation":
            return _Resp({"reservation_id": "TTID_TEST"})
        if stage == "reservation_submit":
            raise ForeUpError(self.submit_error)
        if stage == "probe_validate_only":
            return _Resp({"valid": True})
        return _Resp({})


TEMPLATE = {"booking_fee_required": True, "booking_fee_price": 5,
            "booking_class_id": 50295, "teesheet_side_id": 1, "green_fee": 0}
CODES: list[str] = []


def _code(ttid: str) -> str:
    CODES.append(ttid)
    return "123456"


def _poll_path(client, template):
    return client.book_slot(
        idempotency_key="k", slot={"time": "08:36", "raw_time": "2026-10-17 08:36", "raw": template},
        schedule_id="2432", course_id="19765", date="2026-10-17", players=4,
        customer_id="999", request_code_callback=_code, skip_validation=True,
    )[0]


def _prestage_path(client, template):
    return client.complete_prestaged_hold(
        idempotency_key="k", pending_reservation_id="TTID_TEST",
        target_time="2026-10-17 08:36", template=template,
        schedule_id="2432", course_id="19765", players=4,
        customer_id="999", request_code_callback=_code,
    )


FAILURES = 0


def _fail(msg: str) -> None:
    global FAILURES
    FAILURES += 1
    print(f"  FAIL: {msg}")


def test_both_paths_hand_back_a_payment_link() -> None:
    for path_name, run in (("poll", _poll_path), ("prestage", _prestage_path)):
        for flag in (True, False):
            CODES.clear()
            client = FakeForeUp()
            label = f"{path_name}, booking_fee_required={flag}"
            try:
                out = run(client, {**TEMPLATE, "booking_fee_required": flag})
            except ForeUpError as exc:
                _fail(f"{label}: raised instead of recovering: {str(exc)[-80:]}")
                continue
            if not (out.get("awaiting_payment") and out.get("payment_url")):
                _fail(f"{label}: no payment link in {out}")
                continue
            if out.get("pending_reservation_id") != "TTID_TEST":
                _fail(f"{label}: payment is not for the hold we won: {out}")
                continue
            if len(CODES) != 1:
                _fail(f"{label}: expected one booking-code request, got {len(CODES)}")
                continue
            # A true flag skips the commit ForeUp would refuse; a false one
            # tries it and recovers from the refusal.
            tried_commit = "reservation_submit" in client.calls
            if tried_commit == flag:
                _fail(f"{label}: commit attempted={tried_commit}, calls={client.calls}")
                continue
            print(f"  ok: {label} -> payment link ({' > '.join(client.calls)})")


def test_a_lost_slot_is_not_reported_as_a_fee() -> None:
    """Only the fee refusal recovers. A taken slot must still surface as a loss."""
    for path_name, run in (("poll", _poll_path), ("prestage", _prestage_path)):
        client = FakeForeUp(submit_error=TAKEN)
        try:
            out = run(client, {**TEMPLATE, "booking_fee_required": False})
        except ForeUpError as exc:
            if "Time not available" not in str(exc):
                _fail(f"{path_name}: wrong error surfaced: {exc}")
            else:
                print(f"  ok: {path_name} -> 'Time not available' still raised")
            continue
        _fail(f"{path_name}: a taken slot was turned into {out}")


if __name__ == "__main__":
    for fn in (test_both_paths_hand_back_a_payment_link, test_a_lost_slot_is_not_reported_as_a_fee):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
