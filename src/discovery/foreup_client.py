from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

import httpx


logger = logging.getLogger(__name__)


class ForeUpError(Exception):
    pass


class RetryableHttpError(ForeUpError):
    pass


class StaleSlotError(ForeUpError):
    pass


@dataclass(frozen=True)
class RequestConfig:
    timeout_seconds: float = 8.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.25
    backoff_cap_seconds: float = 2.0


class ForeUpDiscoveryClient:
    _shared_client: Optional[httpx.Client] = None
    _client_lock = Lock()

    def __init__(self, base_url: str, config: Optional[RequestConfig] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.config = config or RequestConfig()
        self.client = self._get_or_create_client(self.config.timeout_seconds)

    @classmethod
    def _get_or_create_client(cls, timeout_seconds: float) -> httpx.Client:
        with cls._client_lock:
            if cls._shared_client is None:
                cls._shared_client = httpx.Client(
                    timeout=httpx.Timeout(timeout_seconds),
                    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
                )
        return cls._shared_client

    def bootstrap_session(self, username: str, password: str, request_id: Optional[str] = None) -> dict[str, Any]:
        req_id = request_id or str(uuid.uuid4())
        self._request("GET", "/api/bootstrap", stage="bootstrap", request_id=req_id, retry_safe=True)
        self._request(
            "POST",
            "/api/auth/login",
            stage="bootstrap",
            request_id=req_id,
            json={"username": username, "password": password},
            retry_safe=False,
        )
        response = self._request("GET", "/api/auth/me", stage="bootstrap", request_id=req_id, retry_safe=True)
        return response.json()

    def lookup_availability(self, course_id: str, date: str, players: int, request_id: Optional[str] = None) -> dict[str, Any]:
        req_id = request_id or str(uuid.uuid4())
        self._request(
            "GET",
            "/api/teesheet",
            stage="availability_lookup",
            request_id=req_id,
            params={"course_id": course_id, "date": date},
            retry_safe=True,
        )
        response = self._request(
            "GET",
            "/api/availability",
            stage="availability_lookup",
            request_id=req_id,
            params={"course_id": course_id, "date": date, "players": players},
            retry_safe=True,
        )
        return response.json()

    def lookup_courses(self, request_id: Optional[str] = None, path: str = "/api/courses") -> list[dict[str, Any]]:
        req_id = request_id or str(uuid.uuid4())
        response = self._request(
            "GET",
            path,
            stage="course_catalog_lookup",
            request_id=req_id,
            retry_safe=True,
        )
        payload = response.json()
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("courses", "results", "items", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def _request(self, method: str, path: str, *, stage: str, request_id: str, retry_safe: bool, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{path}"
        attempts = self.config.max_retries + 1 if retry_safe else 1
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            started_at = datetime.now(timezone.utc).isoformat()
            try:
                response = self.client.request(method, url, **kwargs)
                status = response.status_code

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
