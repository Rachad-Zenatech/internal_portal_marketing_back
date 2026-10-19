"""Google Ads OAuth 2.0 Provider Adapter"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from services.oauth_adapters.base import BaseOAuthProvider


class GoogleAdsOAuthProvider(BaseOAuthProvider):
    provider = "GOOGLE_ADS"
    name = "Google Ads"
    auth_url_base = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    default_scopes = ["https://www.googleapis.com/auth/adwords"]

    def __init__(self):
        client_id = os.getenv("GOOGLE_ADS_CLIENT_ID") or os.getenv("GOOGLE_CLIENT_ID", "")
        client_secret = os.getenv("GOOGLE_ADS_CLIENT_SECRET") or os.getenv("GOOGLE_CLIENT_SECRET", "")
        super().__init__(client_id=client_id, client_secret=client_secret)

    def get_authorization_url(self, state: str, redirect_uri: str, extra_params: dict[str, str] | None = None) -> str:
        extras = {
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        if extra_params:
            extras.update(extra_params)
        return super().get_authorization_url(state=state, redirect_uri=redirect_uri, extra_params=extras)

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
            expires_at = datetime.now(timezone.utc) + timedelta(days=90)
            return {
                "access_token": f"ya29.demo_google_ads_{uuid.uuid4().hex[:20]}",
                "refresh_token": f"1//04demo_refresh_{uuid.uuid4().hex[:24]}",
                "token_type": "Bearer",
                "expires_at": expires_at,
                "scopes": self.default_scopes,
            }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "987-654-3210",
                "account_name": "ZenaTech Group Master Google Ads (MCC)",
                "currency": "USD",
                "time_zone": "America/New_York",
                "is_manager": True,
            },
            {
                "account_id": "412-880-9921",
                "account_name": "DeskFlex US & Canada Search Ads",
                "currency": "USD",
                "time_zone": "America/Chicago",
                "is_manager": False,
            },
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
                "access_token": f"ya29.refreshed_google_ads_{uuid.uuid4().hex[:20]}",
                "expires_at": datetime.now(timezone.utc) + timedelta(days=90),
            }
