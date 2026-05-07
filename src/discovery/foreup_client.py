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

import httpx


logger = logging.getLogger(__name__)

FOREUP_API_KEY = "no_limits"
FOREUP_LOGIN_PATH = "/index.php/api/booking/users/login"
FOREUP_TIMES_PATH = "/index.php/api/booking/times"
FOREUP_RESERVATIONS_PATH = "/index.php/api/booking/users/reservations"

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
    """Convert ForeUp time format (e.g. '7:30 AM', '1:00 PM') to 24h HH:MM."""
    foreup_time = foreup_time.strip()
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


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
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

    def _init_session(self, schedule_id: str = "19765") -> None:
        """Load the booking page to establish a PHPSESSID cookie before login."""
        self.client.get(f"{self.base_url}/index.php/booking/{schedule_id}")

    def bootstrap_session(self, username: str, password: str, schedule_id: str = "", course_id: str = "", request_id: Optional[str] = None) -> dict[str, Any]:
        req_id = request_id or str(uuid.uuid4())
        sid = schedule_id or "19765"
        self._init_session(sid)
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
        params: dict[str, Any] = {
            "time": "all",
            "date": iso_to_foreup_date(date),
            "holes": holes,
            "players": players,
            "schedule_id": schedule_id,
            "specials_only": 0,
            "api_key": FOREUP_API_KEY,
        }
        if booking_class:
            params["booking_class"] = booking_class

        response = self._request(
            "GET",
            FOREUP_TIMES_PATH,
            stage="availability_lookup",
            request_id=req_id,
            params=params,
            retry_safe=True,
        )

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

        slots: list[dict[str, Any]] = []
        if not isinstance(items, list):
            items = []

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

    def _request(self, method: str, path: str, *, stage: str, request_id: str, retry_safe: bool, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{path}"
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
