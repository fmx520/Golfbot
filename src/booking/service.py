from __future__ import annotations

from dataclasses import dataclass

from .interfaces import BookingExecutor, BookingRequest, BookingResult, BookingStrategy, CheckoutExecutor


@dataclass(slots=True)
class DefaultBookingStrategy(BookingStrategy):
    """Composable booking flow so strategy can be replaced independently."""

    checkout: CheckoutExecutor
    booking: BookingExecutor

    def book(self, request: BookingRequest) -> BookingResult:
        context = self.checkout.prepare_checkout(request)
        return self.booking.execute_booking(context)
