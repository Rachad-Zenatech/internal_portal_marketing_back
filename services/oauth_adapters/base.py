"""Base OAuth Provider Adapter Interface

Defines the contract for OAuth 2.0 authorization, token exchange, account discovery,
and token lifecycle across all advertising and social platforms.
"""
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlencode


class BaseOAuthProvider(ABC):
    provider: str
    name: str
    auth_url_base: str
    token_url: str
    default_scopes: list[str]

    def __init__(self, client_id: str = "", client_secret: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret

    @property
    def is_configured(self) -> bool:
        """Returns True if live production client credentials are set in environment."""
        return bool(self.client_id and self.client_secret)

    def get_authorization_url(self, state: str, redirect_uri: str, extra_params: dict[str, str] | None = None) -> str:
        """Generates standard OAuth 2.0 authorization URL."""
        params = {
            "client_id": self.client_id or "zenatech-marketing-client",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.default_scopes),
            "state": state,
        }
        if extra_params:
            params.update(extra_params)
        return f"{self.auth_url_base}?{urlencode(params)}"

    @abstractmethod
    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        """Exchanges authorization code for access & refresh tokens."""
        pass

    @abstractmethod
    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        """Discovers authorized advertising or social accounts associated with the token."""
        pass

    @abstractmethod
    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        """Refreshes an expired access token."""
        pass
