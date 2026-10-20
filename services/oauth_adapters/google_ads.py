"""Google Ads OAuth 2.0 Provider Adapter

GOOGLE_ADS_MODE (services/google_ads_client.py) decides how it works:
- mock: no call to Google. Signing in returns straight to the app with a fake code, tokens are fake, and the accounts
  are the two mock ones. Each connection is stored with connection_metadata {"mode": "mock"}.
- live: the real Google sign-in and the Google Ads API. Without GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET (or
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET) connecting is refused with a plain message instead of storing a fake
  connection. Connections are stored with {"mode": "live"}.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests

from services import google_ads_client
from services.google_ads_client import LIVE_MODE, MOCK_MODE, MOCK_TOKEN_PREFIX, NOT_SET_UP_MESSAGE
from services.google_ads_mock_data import MOCK_ACCOUNTS
from services.oauth_adapters.base import BaseOAuthProvider, OAuthProviderNotReady

INSTANT_IN_LIVE_MESSAGE = (
    "Google Ads is in live mode: use Live OAuth to sign in with Google. Test connections with fake data only work "
    "when GOOGLE_ADS_MODE=mock."
)


class GoogleAdsOAuthProvider(BaseOAuthProvider):
    provider = "GOOGLE_ADS"
    name = "Google Ads"
    auth_url_base = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    default_scopes = ["https://www.googleapis.com/auth/adwords"]

    def __init__(self):
        client_id, client_secret = google_ads_client.google_ads_credentials()
        super().__init__(client_id=client_id, client_secret=client_secret)
        self.mode = google_ads_client.google_ads_mode()

    @property
    def is_mock(self) -> bool:
        return self.mode == MOCK_MODE

    def ensure_ready(self, instant: bool = False) -> None:
        if self.is_mock:
            return
        if not self.is_configured:
            raise OAuthProviderNotReady(NOT_SET_UP_MESSAGE)
        if instant:
            raise OAuthProviderNotReady(INSTANT_IN_LIVE_MESSAGE)

    def connection_metadata(self) -> dict[str, Any]:
        return {"mode": MOCK_MODE if self.is_mock else LIVE_MODE}

    def get_authorization_url(self, state: str, redirect_uri: str, extra_params: dict[str, str] | None = None) -> str:
        if self.is_mock:
            # The mock sign-in: straight back to the app's callback, as if Google had approved it
            separator = "&" if "?" in redirect_uri else "?"
            code = f"mock_google_ads_{uuid.uuid4().hex[:16]}"
            return f"{redirect_uri}{separator}{urlencode({'code': code, 'state': state})}"
        extras = {
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        if extra_params:
            extras.update(extra_params)
        return super().get_authorization_url(state=state, redirect_uri=redirect_uri, extra_params=extras)

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        if self.is_mock:
            expires_at = datetime.now(timezone.utc) + timedelta(days=90)
            return {
                "access_token": f"{MOCK_TOKEN_PREFIX}google_ads_{uuid.uuid4().hex[:20]}",
                "refresh_token": f"1//04demo_refresh_{uuid.uuid4().hex[:24]}",
                "token_type": "Bearer",
                "expires_at": expires_at,
                "scopes": self.default_scopes,
            }
        self.ensure_ready()

        def _post():
            return requests.post(
                self.token_url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
                timeout=15.0,
            )
        resp = await asyncio.to_thread(_post)
        resp.raise_for_status()
        data = resp.json()
        expires_in = data.get("expires_in", 3600)
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "token_type": data.get("token_type", "Bearer"),
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            "scopes": data.get("scope", "").split() or self.default_scopes,
        }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        if self.is_mock:
            return [dict(account) for account in MOCK_ACCOUNTS]
        return await google_ads_client.discover_accounts(access_token)

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        if self.is_mock:
            return {
                "access_token": f"{MOCK_TOKEN_PREFIX}refreshed_google_ads_{uuid.uuid4().hex[:20]}",
                "expires_at": datetime.now(timezone.utc) + timedelta(days=90),
            }
        self.ensure_ready()

        def _post():
            return requests.post(
                self.token_url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=15.0,
            )
        resp = await asyncio.to_thread(_post)
        resp.raise_for_status()
        data = resp.json()
        expires_in = data.get("expires_in", 3600)
        return {
            "access_token": data["access_token"],
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        }
