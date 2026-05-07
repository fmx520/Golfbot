"""Anti-Captcha reCAPTCHA v2 solver using raw HTTP calls."""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.anti-captcha.com"


class CaptchaSolveError(Exception):
    pass


class CaptchaSolveTimeout(CaptchaSolveError):
    pass


class AntiCaptchaSolver:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._client = httpx.Client(timeout=15)

    def create_task(self, website_url: str, site_key: str) -> str:
        response = self._client.post(
            f"{API_BASE}/createTask",
            json={
                "clientKey": self.api_key,
                "task": {
                    "type": "RecaptchaV2TaskProxyless",
                    "websiteURL": website_url,
                    "websiteKey": site_key,
                },
            },
        )
        data = response.json()
        if data.get("errorId", 0) != 0:
            raise CaptchaSolveError(
                f"Anti-Captcha createTask failed: {data.get('errorCode')} — {data.get('errorDescription')}"
            )
        task_id = str(data["taskId"])
        logger.info("anticaptcha_task_created", extra={"task_id": task_id})
        return task_id

    def get_result(self, task_id: str) -> Optional[str]:
        response = self._client.post(
            f"{API_BASE}/getTaskResult",
            json={"clientKey": self.api_key, "taskId": int(task_id)},
        )
        data = response.json()
        if data.get("errorId", 0) != 0:
            raise CaptchaSolveError(
                f"Anti-Captcha getTaskResult failed: {data.get('errorCode')} — {data.get('errorDescription')}"
            )
        if data.get("status") == "ready":
            return data["solution"]["gRecaptchaResponse"]
        return None

    def solve(self, website_url: str, site_key: str, timeout: float = 120) -> str:
        task_id = self.create_task(website_url, site_key)
        deadline = time.monotonic() + timeout
        time.sleep(5)
        while time.monotonic() < deadline:
            token = self.get_result(task_id)
            if token:
                logger.info("anticaptcha_solved", extra={"task_id": task_id})
                return token
            time.sleep(3)
        raise CaptchaSolveTimeout(f"CAPTCHA solve timed out after {timeout}s (task {task_id})")

    def close(self) -> None:
        self._client.close()
