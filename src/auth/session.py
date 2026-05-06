from __future__ import annotations

from dataclasses import dataclass

from .interfaces import CookieStore, SessionClient, TokenProvider


@dataclass(slots=True)
class AuthSessionManager:
    """Coordinates token and cookie bootstrapping for downstream modules."""

    token_provider: TokenProvider
    cookie_store: CookieStore
    session_client: SessionClient

    def bootstrap(self) -> SessionClient:
        token = self.token_provider.get_token()
        self.session_client.with_auth(token)
        self.session_client.with_cookies(self.cookie_store.load())
        return self.session_client

    def persist_cookies(self, cookies: dict[str, str]) -> None:
        self.cookie_store.save(cookies)
