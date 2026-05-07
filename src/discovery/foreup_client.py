from __future__ import annotations

import logging
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

import json as _json

import httpx


logger = logging.getLogger(__name__)

FOREUP_API_KEY = "no_limits"
FOREUP_LOGIN_PATH = "/index.php/api/booking/users/login"
FOREUP_TIMES_PATH = "/index.php/api/booking/times"
FOREUP_RESERVATIONS_PATH = "/index.php/api/booking/users/reservations"
FOREUP_PENDING_RESERVATION_PATH = "/index.php/api/booking/pending_reservation"

ANTIBOT_MARKERS = (
    "g-recaptcha",
    "recaptcha",
    "captcha-challenge",
    "id.me",
    "verify your identity",
)


class ForeUpError(Exception):
    pass


class RetryableHttpError(ForeUpError):
    pass


class StaleSlotError(ForeUpError):
    pass


class AntiBotDetectedError(ForeUpError):
    pass


@dataclass(frozen=True)
class RequestConfig:
    timeout_seconds: float = 8.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.25
    backoff_cap_seconds: float = 2.0


def iso_to_foreup_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to MM-DD-YYYY for ForeUp API."""
    parts = iso_date.split("-")
    if len(parts) != 3:
        raise ValueError(f"Expected YYYY-MM-DD date, got {iso_date!r}")
    return f"{parts[1]}-{parts[2]}-{parts[0]}"


def foreup_time_to_24h(foreup_time: str) -> str:
    """Convert ForeUp time format to 24h HH:MM.

    Handles: '7:30 AM', '1:00 PM', '14:09', '2026-05-09 14:09'.
    """
    foreup_time = foreup_time.strip()
    dt_match = re.match(r"^\d{4}-\d{2}-\d{2}\s+(\d{2}:\d{2})", foreup_time)
    if dt_match:
        return dt_match.group(1)
    match = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)$", foreup_time, re.IGNORECASE)
    if not match:
        if re.match(r"^\d{2}:\d{2}$", foreup_time):
            return foreup_time
        raise ValueError(f"Cannot parse ForeUp time: {foreup_time!r}")
    hour, minute, period = int(match.group(1)), match.group(2), match.group(3).upper()
    if period == "PM" and hour != 12:
        hour += 12
    elif period == "AM" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute}"


_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

BROWSER_HEADERS = {
    "User-Agent": random.choice(_USER_AGENTS),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://foreupsoftware.com",
    "Referer": "https://foreupsoftware.com/index.php/booking/",
}


class ForeUpDiscoveryClient:
    _shared_client: Optional[httpx.Client] = None
    _client_lock = Lock()

    def __init__(self, base_url: str, config: Optional[RequestConfig] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.config = config or RequestConfig()
        self.client = self._get_or_create_client(self.config.timeout_seconds)
        self._jwt: Optional[str] = None
        self._booking_classes: list[str] = []
        self._booking_class_info: list[dict[str, str]] = []
        self._recaptcha_sitekey: Optional[str] = None
        self._last_page_html: Optional[str] = None

    @classmethod
    def _get_or_create_client(cls, timeout_seconds: float) -> httpx.Client:
        with cls._client_lock:
            if cls._shared_client is None:
                cls._shared_client = httpx.Client(
                    timeout=httpx.Timeout(timeout_seconds),
                    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
                    follow_redirects=True,
                    headers=BROWSER_HEADERS,
                )
        return cls._shared_client

    def _init_session(self, schedule_id: str = "19765", teesheet_id: str = "") -> None:
        """Load the booking page to establish a PHPSESSID cookie and extract booking classes."""
        response = self.client.get(f"{self.base_url}/index.php/booking/{schedule_id}")
        self._last_page_html = response.text
        self._booking_classes = self._extract_booking_classes(response.text, teesheet_id)
        self._recaptcha_sitekey = self._extract_sitekey(response.text)
        logger.info("extracted_booking_classes", extra={
            "teesheet_id": teesheet_id,
            "booking_classes": self._booking_classes,
            "recaptcha_sitekey": self._recaptcha_sitekey,
        })

    def _extract_booking_classes(self, html: str, teesheet_id: str) -> list[str]:
        m = re.search(r"SCHEDULES\s*=\s*(\[.*?\])\s*;", html, re.DOTALL)
        if not m:
            return self._extract_booking_classes_fallback(html)
        try:
            schedules = _json.loads(m.group(1))
        except (ValueError, _json.JSONDecodeError):
            return self._extract_booking_classes_fallback(html)

        for sched in schedules:
            if teesheet_id and sched.get("teesheet_id") != teesheet_id:
                continue
            bcs = sched.get("booking_classes", [])
            if not bcs:
                continue
            visible_bcs = [
                bc for bc in bcs
                if bc.get("active") == "1" and bc.get("hidden") != "1"
            ]
            if visible_bcs:
                unprotected = [bc for bc in visible_bcs if bc.get("online_booking_protected") != "1"]
                ordered = unprotected + [bc for bc in visible_bcs if bc not in unprotected]
                self._booking_class_info = [
                    {"id": bc["booking_class_id"], "name": bc.get("booking_class_name", bc.get("name", f"Class {bc['booking_class_id']}"))}
                    for bc in ordered
                ]
                return [bc["booking_class_id"] for bc in ordered]
        return self._extract_booking_classes_fallback(html)

    def _extract_booking_classes_fallback(self, html: str) -> list[str]:
        raw = re.findall(r'"booking_class_id"\s*:\s*"(\d+)"', html)
        seen: set[str] = set()
        return [bc for bc in raw if bc not in seen and not seen.add(bc)]

    @staticmethod
    def _extract_sitekey(html: str) -> Optional[str]:
        m = re.search(r'data-sitekey=["\']([^"\']+)["\']', html)
        if m:
            return m.group(1)
        m = re.search(r"grecaptcha\.render\([^,]+,\s*\{[^}]*['\"]sitekey['\"]\s*:\s*['\"]([^'\"]+)['\"]", html)
        return m.group(1) if m else None

    def get_booking_class_options(self) -> list[dict[str, str]]:
        return self._booking_class_info

    def extract_recaptcha_sitekey(self, schedule_id: str = "") -> Optional[str]:
        if self._recaptcha_sitekey:
            return self._recaptcha_sitekey
        if not self._last_page_html and schedule_id:
            response = self.client.get(f"{self.base_url}/index.php/booking/{schedule_id}")
            self._last_page_html = response.text
            self._recaptcha_sitekey = self._extract_sitekey(response.text)
        return self._recaptcha_sitekey

    def bootstrap_session(self, username: str, password: str, schedule_id: str = "", course_id: str = "", request_id: Optional[str] = None) -> dict[str, Any]:
        req_id = request_id or str(uuid.uuid4())
        sid = schedule_id or "19765"
        self._init_session(sid, teesheet_id=course_id)
        response = self._request(
            "POST",
            FOREUP_LOGIN_PATH,
            stage="bootstrap",
            request_id=req_id,
            data={
                "username": username,
                "password": password,
                "api_key": FOREUP_API_KEY,
                "booking_class_id": "",
                "course_id": sid,
            },
            retry_safe=False,
        )
        try:
            result = response.json()
        except Exception:
            return {"status": "logged_in", "raw": response.text[:500]}

        if isinstance(result, dict) and result.get("jwt"):
            self._jwt = result["jwt"]
            self.client.headers["X-Authorization"] = f"Bearer {self._jwt}"

        return result

    def _parse_times_response(self, response: httpx.Response, schedule_id: str) -> list[dict[str, Any]]:
        try:
            raw = response.json()
        except Exception:
            return []

        if isinstance(raw, bool) or raw is None:
            return []
        if isinstance(raw, dict):
            if not raw.get("success", True):
                return []
            items = raw.get("times", raw.get("slots", []))
        elif isinstance(raw, list):
            items = raw
        else:
            return []

        if not isinstance(items, list):
            return []

        slots: list[dict[str, Any]] = []
        for item in items:
            available = int(item.get("available_spots", item.get("spots", item.get("openSpots", 0))))
            if available < 1:
                continue

            raw_time = str(item.get("time", item.get("starts_at", item.get("teeTime", ""))))
            try:
                normalized_time = foreup_time_to_24h(raw_time)
            except ValueError:
                normalized_time = raw_time

            slots.append({
                "time": normalized_time,
                "raw_time": raw_time,
                "available_spots": available,
                "green_fee": item.get("green_fee", item.get("greenFee", item.get("price", ""))),
                "tee_time_id": item.get("tee_time_id", item.get("teeTimeId", item.get("id", ""))),
                "teesheet_id": item.get("teesheet_id", item.get("teesheetId", "")),
                "booking_class_id": item.get("booking_class_id", item.get("bookingClassId", "")),
                "schedule_id": schedule_id,
                "raw": item,
            })

        slots.sort(key=lambda s: s["time"])
        return slots

    def _fetch_times_for_class(
        self,
        params: dict[str, Any],
        request_id: str,
        schedule_id: str,
    ) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            FOREUP_TIMES_PATH,
            stage="availability_lookup",
            request_id=request_id,
            params=params,
            retry_safe=True,
        )
        return self._parse_times_response(response, schedule_id)

    def lookup_availability(
        self,
        *,
        schedule_id: str,
        date: str,
        players: int,
        holes: int = 18,
        booking_class: str = "",
        request_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        req_id = request_id or str(uuid.uuid4())
        base_params: dict[str, Any] = {
            "time": "all",
            "date": iso_to_foreup_date(date),
            "holes": holes,
            "players": players,
            "schedule_id": schedule_id,
            "specials_only": 0,
            "api_key": FOREUP_API_KEY,
        }

        if booking_class:
            base_params["booking_class"] = booking_class
            return self._fetch_times_for_class(base_params, req_id, schedule_id)

        if self._booking_classes:
            for bc in self._booking_classes:
                params = {**base_params, "booking_class": bc}
                try:
                    result = self._fetch_times_for_class(params, req_id, schedule_id)
                    if result:
                        return result
                except ForeUpError:
                    continue
            return []

        return self._fetch_times_for_class(base_params, req_id, schedule_id)

    def refresh_session(self, schedule_id: str = "", course_id: str = "", username: str = "", password: str = "", request_id: Optional[str] = None) -> None:
        """Re-warm cookies and JWT before a sniper release window."""
        req_id = request_id or str(uuid.uuid4())
        sid = schedule_id or "19765"
        self._init_session(sid, teesheet_id=course_id)
        if username and password:
            self.bootstrap_session(username=username, password=password, schedule_id=sid, course_id=course_id, request_id=req_id)

    def lookup_availability_fast(
        self,
        *,
        schedule_id: str,
        date: str,
        players: int,
        holes: int = 18,
        booking_class: str = "",
        request_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Stripped-down lookup for sniper polling: cached booking class, no retries, short timeout."""
        req_id = request_id or str(uuid.uuid4())
        params: dict[str, Any] = {
            "time": "all",
            "date": iso_to_foreup_date(date),
            "holes": holes,
            "players": players,
            "schedule_id": schedule_id,
            "specials_only": 0,
            "api_key": FOREUP_API_KEY,
        }

        bc = booking_class or (self._booking_classes[0] if self._booking_classes else "")
        if bc:
            params["booking_class"] = bc

        response = self._request(
            "GET",
            FOREUP_TIMES_PATH,
            stage="sniper_poll",
            request_id=req_id,
            params=params,
            retry_safe=False,
        )
        return self._parse_times_response(response, schedule_id)

    def _request(self, method: str, path: str, *, stage: str, request_id: str, retry_safe: bool, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{path}"
        self.client.headers["User-Agent"] = random.choice(_USER_AGENTS)
        if stage not in ("sniper_poll",):
            time.sleep(random.uniform(0.01, 0.08))
        attempts = self.config.max_retries + 1 if retry_safe else 1
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            started_at = datetime.now(timezone.utc).isoformat()
            try:
                response = self.client.request(method, url, **kwargs)
                status = response.status_code

                self._check_antibot(response)

                if status == 401:
                    raise ForeUpError(f"Authentication failed: {method} {url} returned 401")

                if status in (429,) or 500 <= status <= 599:
                    if retry_safe and attempt < attempts:
                        delay = self._retry_delay(attempt)
                        self._log(stage, request_id, started_at, started, "retry", attempt, status, delay)
                        time.sleep(delay)
                        continue
                    raise RetryableHttpError(f"{method} {url} failed with status {status}")

                response.raise_for_status()
                self._log(stage, request_id, started_at, started, "ok", attempt, status, None)
                return response
            except AntiBotDetectedError:
                self._log(stage, request_id, started_at, started, "antibot", attempt, 0, None)
                raise
            except httpx.HTTPStatusError as exc:
                body_preview = exc.response.text[:4000] if exc.response else ""
                self._log(stage, request_id, started_at, started, "error", attempt, exc.response.status_code if exc.response else 0, None)
                raise ForeUpError(f"{exc} | Body: {body_preview}") from exc
            except httpx.TimeoutException as exc:
                if retry_safe and attempt < attempts:
                    delay = self._retry_delay(attempt)
                    self._log(stage, request_id, started_at, started, "retry", attempt, 0, delay)
                    time.sleep(delay)
                    continue
                self._log(stage, request_id, started_at, started, "error", attempt, 0, None)
                raise ForeUpError("request timed out") from exc
            except httpx.HTTPError as exc:
                self._log(stage, request_id, started_at, started, "error", attempt, 0, None)
                raise ForeUpError(str(exc)) from exc

        raise ForeUpError("unexpected retry exhaustion")

    def _check_antibot(self, response: httpx.Response) -> None:
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            body_lower = response.text[:5000].lower()
            for marker in ANTIBOT_MARKERS:
                if marker in body_lower:
                    raise AntiBotDetectedError(
                        f"Anti-bot protection detected ({marker}). "
                        "This course may require browser-based booking."
                    )

    def _retry_delay(self, attempt: int) -> float:
        exp = min(self.config.backoff_cap_seconds, self.config.backoff_base_seconds * (2 ** (attempt - 1)))
        jitter = random.uniform(0.0, exp * 0.25)
        return exp + jitter

    def _log(self, stage: str, request_id: str, started_at: str, started: float, result: str, attempt: int, status: int, delay: Optional[float]) -> None:
        ended_at = datetime.now(timezone.utc).isoformat()
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "foreup_request",
            extra={
                "request_id": request_id,
                "stage": stage,
                "started_at": started_at,
                "ended_at": ended_at,
                "latency_ms": latency_ms,
                "attempt": attempt,
                "http_status": status,
                "result": result,
                "retry_delay_s": delay,
            },
        )
