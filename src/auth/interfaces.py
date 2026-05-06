from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(slots=True)
class SessionToken:
    """Authentication token metadata used by transport layers."""

    access_token: str
    expires_at: datetime


class TokenProvider(Protocol):
    """Strategy for acquiring and refreshing API/session tokens."""

    def get_token(self) -> SessionToken:
        ...

    def refresh_token(self) -> SessionToken:
        ...


class CookieStore(Protocol):
    """Persistence contract for browser-like cookie jars."""

    def load(self) -> dict[str, str]:
        ...

    def save(self, cookies: dict[str, str]) -> None:
        ...


class SessionClient(Protocol):
    """HTTP/session abstraction used by discovery and booking services."""

    def with_auth(self, token: SessionToken) -> None:
        ...

    def with_cookies(self, cookies: dict[str, str]) -> None:
        ...

    def get(self, path: str, params: dict[str, object] | None = None) -> dict:
        ...

    def post(self, path: str, payload: dict[str, object]) -> dict:
        ...
