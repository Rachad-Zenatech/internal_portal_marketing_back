"""Microsoft Ads (Bing) OAuth 2.0 Provider Adapter

MICROSOFT_ADS_MODE (services/microsoft_ads_client.py) decides how it works:
- mock: no call to Microsoft. Signing in returns straight to the app with a fake code, tokens are fake, and the
  accounts are the mock ones. Each connection is stored with connection_metadata {"mode": "mock"}.
- live: the real Microsoft sign-in and the Microsoft Advertising API. Without MICROSOFT_ADS_CLIENT_ID /
  MICROSOFT_ADS_CLIENT_SECRET connecting is refused with a plain message. Connections are stored with {"mode": "live"}.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests

from services import microsoft_ads_client
from services.microsoft_ads_client import LIVE_MODE, MOCK_MODE, MOCK_TOKEN_PREFIX, NOT_SET_UP_MESSAGE
from services.microsoft_ads_mock_data import MOCK_ACCOUNTS
from services.oauth_adapters.base import BaseOAuthProvider, OAuthProviderNotReady

INSTANT_IN_LIVE_MESSAGE = (
    "Microsoft Ads is in live mode: use Live OAuth to sign in with Microsoft. Test connections with fake data only "
    "work when MICROSOFT_ADS_MODE=mock."
)


class MicrosoftAdsOAuthProvider(BaseOAuthProvider):
    provider = "MICROSOFT_ADS"
    name = "Microsoft Ads"
    auth_url_base = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    default_scopes = ["https://ads.microsoft.com/msads.manage", "offline_access"]

    def __init__(self):
        # Only the Microsoft Ads app's own credentials: MICROSOFT_CLIENT_ID / _SECRET are the portal's sign-in (SSO)
        # app and must never be used here
        client_id, client_secret = microsoft_ads_client.microsoft_ads_credentials()
        super().__init__(client_id=client_id, client_secret=client_secret)
        self.mode = microsoft_ads_client.microsoft_ads_mode()

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
            # The mock sign-in: straight back to the app's callback, as if Microsoft had approved it
            separator = "&" if "?" in redirect_uri else "?"
            code = f"mock_microsoft_ads_{uuid.uuid4().hex[:16]}"
            return f"{redirect_uri}{separator}{urlencode({'code': code, 'state': state})}"
        extras = {"prompt": "select_account"}
        if extra_params:
            extras.update(extra_params)
        return super().get_authorization_url(state=state, redirect_uri=redirect_uri, extra_params=extras)

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        def _post():
            return requests.post(
                self.token_url,
                data={"client_id": self.client_id, "client_secret": self.client_secret,
                      "scope": " ".join(self.default_scopes), **data},
                timeout=15.0,
            )
        resp = await asyncio.to_thread(_post)
        resp.raise_for_status()
        return resp.json()

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        if self.is_mock:
            return {
                "access_token": f"{MOCK_TOKEN_PREFIX}ads_{uuid.uuid4().hex[:20]}",
                "refresh_token": f"M.R3_msft_refresh_{uuid.uuid4().hex[:24]}",
                "token_type": "Bearer",
                "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
                "scopes": self.default_scopes,
            }
        self.ensure_ready()
        data = await self._token_request({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri})
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "token_type": data.get("token_type", "Bearer"),
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600)),
            "scopes": data.get("scope", "").split() or self.default_scopes,
        }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        if self.is_mock:
            return [dict(account) for account in MOCK_ACCOUNTS]
        return await microsoft_ads_client.discover_accounts(access_token)

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        if self.is_mock:
            return {
                "access_token": f"{MOCK_TOKEN_PREFIX}refreshed_{uuid.uuid4().hex[:20]}",
                "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
            }
        self.ensure_ready()
        data = await self._token_request({"refresh_token": refresh_token, "grant_type": "refresh_token"})
        return {
            "access_token": data["access_token"],
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600)),
        }
