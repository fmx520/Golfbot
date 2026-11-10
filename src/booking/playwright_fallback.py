"""Browser-automation fallback for ForeUp courses with anti-bot protections.

Used when the direct API returns reCAPTCHA or other challenges. Runs in headless
mode by default (set PLAYWRIGHT_HEADLESS=false for debugging).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlaywrightSlot:
    time: str
    available_spots: int
    green_fee: str
    element_index: int
    raw_text: str


class PlaywrightBookingError(Exception):
    pass


class PlaywrightBookingClient:
    """Browser-based ForeUp booking client using Playwright."""

    def __init__(self, headless: Optional[bool] = None) -> None:
        if headless is None:
            headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
        self._headless = headless
        self._browser = None
        self._page = None

    def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._page

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise PlaywrightBookingError(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            )

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        self._page = self._browser.new_page()
        return self._page

    def login(self, booking_url: str, username: str, password: str) -> bool:
        page = self._ensure_browser()
        logger.info("playwright_navigate", extra={"url": booking_url})
        page.goto(booking_url, wait_until="networkidle", timeout=30000)

        login_button = page.query_selector("button:has-text('Log In'), a:has-text('Log In'), .login-btn, #login-btn")
        if login_button:
            login_button.click()
            page.wait_for_timeout(1000)

        username_field = page.query_selector(
            "input[name='username'], input[name='email'], input[type='email'], "
            "input[placeholder*='email' i], input[placeholder*='username' i]"
        )
        password_field = page.query_selector(
            "input[name='password'], input[type='password']"
        )

        if not username_field or not password_field:
            raise PlaywrightBookingError("Could not find login form fields on the page")

        username_field.fill(username)
        password_field.fill(password)

        submit = page.query_selector(
            "button[type='submit'], input[type='submit'], "
            "button:has-text('Log In'), button:has-text('Sign In')"
        )
        if submit:
            submit.click()
        else:
            password_field.press("Enter")

        page.wait_for_timeout(3000)

        if page.query_selector(".error-message, .alert-danger, .login-error"):
            raise PlaywrightBookingError("Login failed — check credentials")

        logger.info("playwright_login_success")
        return True

    def find_times(self, date: str, players: int) -> list[PlaywrightSlot]:
        page = self._ensure_browser()

        date_input = page.query_selector(
            "input[name='date'], input[type='date'], .date-picker input, #date-field"
        )
        if date_input:
            date_input.fill(date)
            page.wait_for_timeout(2000)

        page.wait_for_timeout(3000)

        slot_elements = page.query_selector_all(
            ".time-slot, .tee-time, .booking-slot, "
            "tr.available, .tee-time-row, [data-time]"
        )

        slots: list[PlaywrightSlot] = []
        for i, el in enumerate(slot_elements):
            text = el.inner_text().strip()
            time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM)?)", text, re.IGNORECASE)
            spots_match = re.search(r"(\d+)\s*(?:spot|player|available|open)", text, re.IGNORECASE)
            fee_match = re.search(r"\$[\d,.]+", text)

            if not time_match:
                continue

            available = int(spots_match.group(1)) if spots_match else 1
            if available < players:
                continue

            slots.append(PlaywrightSlot(
                time=time_match.group(1).strip(),
                available_spots=available,
                green_fee=fee_match.group(0) if fee_match else "",
                element_index=i,
                raw_text=text,
            ))

        logger.info("playwright_found_slots", extra={"count": len(slots)})
        return slots

    def book_slot(self, slot: PlaywrightSlot, dry_run: bool = True) -> dict[str, Any]:
        page = self._ensure_browser()

        slot_elements = page.query_selector_all(
            ".time-slot, .tee-time, .booking-slot, "
            "tr.available, .tee-time-row, [data-time]"
        )

        if slot.element_index >= len(slot_elements):
            raise PlaywrightBookingError(f"Slot element {slot.element_index} no longer exists")

        target = slot_elements[slot.element_index]

        if dry_run:
            logger.info("playwright_dry_run", extra={"slot": slot.time})
            return {"status": "dry_run", "slot": slot.time, "message": "Dry run — booking not submitted"}

        book_btn = target.query_selector(
            "button:has-text('Book'), button:has-text('Reserve'), "
            "a:has-text('Book'), .book-btn"
        )
        if book_btn:
            book_btn.click()
        else:
            target.click()

        page.wait_for_timeout(3000)

        confirm_btn = page.query_selector(
            "button:has-text('Confirm'), button:has-text('Complete'), "
            "button:has-text('Submit'), .confirm-btn, #confirm-booking"
        )
        if confirm_btn:
            confirm_btn.click()
            page.wait_for_timeout(5000)

        success_indicators = page.query_selector_all(
            ".confirmation, .success, .booking-confirmed, "
            ":has-text('Confirmation'), :has-text('Booked')"
        )
        if success_indicators:
            confirmation_text = success_indicators[0].inner_text().strip()
            logger.info("playwright_booking_confirmed", extra={"slot": slot.time})
            return {"status": "confirmed", "slot": slot.time, "confirmation": confirmation_text}

        return {"status": "unknown", "slot": slot.time, "message": "Booking submitted but confirmation unclear"}

    def close(self) -> None:
        if self._browser:
            self._browser.close()
            self._browser = None
        if hasattr(self, "_playwright") and self._playwright:
            self._playwright.stop()
            self._playwright = None
