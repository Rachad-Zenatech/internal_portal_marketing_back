"""Microsoft Ads (Bing) OAuth 2.0 Provider Adapter"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from services.oauth_adapters.base import BaseOAuthProvider


class MicrosoftAdsOAuthProvider(BaseOAuthProvider):
    provider = "MICROSOFT_ADS"
    name = "Microsoft Ads"
    auth_url_base = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    default_scopes = ["https://ads.microsoft.com/msads.manage", "offline_access"]

    def __init__(self):
        # Only the Microsoft Ads app's own credentials: MICROSOFT_CLIENT_ID / _SECRET are the portal's sign-in (SSO)
        # app and must never be used here
        client_id = os.getenv("MICROSOFT_ADS_CLIENT_ID", "")
        client_secret = os.getenv("MICROSOFT_ADS_CLIENT_SECRET", "")
        super().__init__(client_id=client_id, client_secret=client_secret)

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        if self.is_configured:
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
        else:
            expires_at = datetime.now(timezone.utc) + timedelta(days=60)
            return {
                "access_token": f"EwBoA_msft_ads_{uuid.uuid4().hex[:20]}",
                "refresh_token": f"M.R3_msft_refresh_{uuid.uuid4().hex[:24]}",
                "token_type": "Bearer",
                "expires_at": expires_at,
                "scopes": self.default_scopes,
            }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "MS-8842-109",
                "account_name": "ZenaTech Global Bing Ads Account",
                "currency": "USD",
                "time_zone": "EasternTime",
            }
        ]

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        if self.is_configured:
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
        else:
            return {
                "access_token": f"EwBoA_msft_refreshed_{uuid.uuid4().hex[:20]}",
                "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
            }
