from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from discovery.interfaces import TeeTime


@dataclass(slots=True)
class BookingRequest:
    tee_time: TeeTime
    payment_profile_id: str
    contact_email: str


@dataclass(slots=True)
class BookingResult:
    success: bool
    confirmation_id: str | None = None
    error: str | None = None


class CheckoutExecutor(Protocol):
    def prepare_checkout(self, request: BookingRequest) -> dict[str, object]:
        ...


class BookingExecutor(Protocol):
    def execute_booking(self, checkout_context: dict[str, object]) -> BookingResult:
        ...


class BookingStrategy(Protocol):
    def book(self, request: BookingRequest) -> BookingResult:
        ...
