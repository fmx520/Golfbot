"""Configuration loading, validation, and safe logging helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import copy
import logging
import os
import re

import yaml

TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

DEFAULT_RETRY_PROFILE = {
    "max_attempts": 5,
    "backoff_seconds": 2,
    "max_backoff_seconds": 30,
}

DEFAULT_COURSE_SETTINGS = {
    "poll_interval_seconds": 30,
    "target_windows": [],
    "retry_profile": DEFAULT_RETRY_PROFILE,
    "player_count": 4,
}

DEFAULT_SECRET_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "cookies",
    "authorization",
    "password",
    "secret",
    "api_key",
    "session",
    "email",
    "phone",
    "name",
}


@dataclass(frozen=True)
class RetryProfile:
    max_attempts: int = 5
    backoff_seconds: int = 2
    max_backoff_seconds: int = 30


@dataclass(frozen=True)
class TargetWindow:
    start: str
    end: str


@dataclass(frozen=True)
class CourseSettings:
    poll_interval_seconds: int = 30
    target_windows: tuple[TargetWindow, ...] = ()
    retry_profile: RetryProfile = RetryProfile()
    player_count: int = 4


@dataclass(frozen=True)
class AppConfig:
    defaults: CourseSettings
    courses: dict[str, CourseSettings]


def _validate_time(value: str, field: str) -> None:
    if not isinstance(value, str) or not TIME_PATTERN.match(value):
        raise ValueError(f"{field} must be HH:MM in 24-hour format, got {value!r}")


def _parse_windows(windows: Any) -> tuple[TargetWindow, ...]:
    if windows is None:
        return ()
    if not isinstance(windows, list):
        raise ValueError("target_windows must be a list")

    parsed: list[TargetWindow] = []
    for i, window in enumerate(windows):
        if not isinstance(window, dict):
            raise ValueError(f"target_windows[{i}] must be a mapping")
        start = window.get("start")
        end = window.get("end")
        _validate_time(start, f"target_windows[{i}].start")
        _validate_time(end, f"target_windows[{i}].end")
        parsed.append(TargetWindow(start=start, end=end))
    return tuple(parsed)


def _parse_retry_profile(value: Any) -> RetryProfile:
    data = copy.deepcopy(DEFAULT_RETRY_PROFILE)
    if value is not None:
        if not isinstance(value, dict):
            raise ValueError("retry_profile must be a mapping")
        data.update(value)

    for key in ("max_attempts", "backoff_seconds", "max_backoff_seconds"):
        if not isinstance(data[key], int) or data[key] <= 0:
            raise ValueError(f"retry_profile.{key} must be a positive integer")

    return RetryProfile(
        max_attempts=data["max_attempts"],
        backoff_seconds=data["backoff_seconds"],
        max_backoff_seconds=data["max_backoff_seconds"],
    )


def _parse_course_settings(value: Any, base: dict[str, Any] | None = None) -> CourseSettings:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("course settings must be a mapping")

    merged = copy.deepcopy(DEFAULT_COURSE_SETTINGS)
    if base:
        merged.update(base)
    merged.update(value)

    poll_interval = merged["poll_interval_seconds"]
    if not isinstance(poll_interval, int) or poll_interval <= 0:
        raise ValueError("poll_interval_seconds must be a positive integer")

    player_count = merged["player_count"]
    if not isinstance(player_count, int) or not (1 <= player_count <= 4):
        raise ValueError("player_count must be an integer between 1 and 4")

    return CourseSettings(
        poll_interval_seconds=poll_interval,
        target_windows=_parse_windows(merged.get("target_windows")),
        retry_profile=_parse_retry_profile(merged.get("retry_profile")),
        player_count=player_count,
    )


def load_config(path: str | Path = "config/courses.yaml") -> AppConfig:
    """Load YAML config from disk and apply validation/defaulting."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError("Top-level config must be a mapping")

    defaults_raw = raw.get("defaults") or {}
    defaults = _parse_course_settings(defaults_raw)

    courses_raw = raw.get("courses") or {}
    if not isinstance(courses_raw, dict):
        raise ValueError("courses must be a mapping")

    base = {
        "poll_interval_seconds": defaults.poll_interval_seconds,
        "target_windows": [
            {"start": w.start, "end": w.end} for w in defaults.target_windows
        ],
        "retry_profile": {
            "max_attempts": defaults.retry_profile.max_attempts,
            "backoff_seconds": defaults.retry_profile.backoff_seconds,
            "max_backoff_seconds": defaults.retry_profile.max_backoff_seconds,
        },
        "player_count": defaults.player_count,
    }

    courses = {
        course_name: _parse_course_settings(course_settings, base=base)
        for course_name, course_settings in courses_raw.items()
    }

    return AppConfig(defaults=defaults, courses=courses)


class RedactSecretsFilter(logging.Filter):
    """Redacts secrets and personally identifiable fields from log records."""

    def __init__(self, extra_secret_keys: set[str] | None = None) -> None:
        super().__init__()
        self.secret_keys = set(DEFAULT_SECRET_KEYS)
        if extra_secret_keys:
            self.secret_keys.update(k.lower() for k in extra_secret_keys)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_obj(record.msg, self.secret_keys)
        if isinstance(record.args, dict):
            record.args = _redact_obj(record.args, self.secret_keys)
        elif isinstance(record.args, tuple):
            record.args = tuple(_redact_obj(a, self.secret_keys) for a in record.args)
        return True


def configure_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Create a logger that redacts sensitive data before emitting logs."""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not any(isinstance(f, RedactSecretsFilter) for f in logger.filters):
        logger.addFilter(RedactSecretsFilter())

    return logger


def env_secrets() -> dict[str, str]:
    """Read environment secrets without logging raw values."""
    keys = ["API_TOKEN", "SESSION_COOKIE", "USER_EMAIL", "USER_PHONE", "PLAYER_NAME"]
    return {k: os.getenv(k, "") for k in keys}


def _redact_obj(value: Any, secret_keys: set[str]) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, raw_val in value.items():
            key_str = str(key).lower()
            if any(marker in key_str for marker in secret_keys):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_obj(raw_val, secret_keys)
        return redacted
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_obj(v, secret_keys) for v in value)
    if isinstance(value, str):
        # crude fallback masking for bearer tokens and long cookie-like strings
        value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?i)(cookie=)[^;\s]+", r"\1[REDACTED]", value)
        return value
    return value
