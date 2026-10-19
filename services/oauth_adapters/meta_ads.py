"""Meta Ads (Facebook & Instagram) OAuth 2.0 Provider Adapter"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from services.oauth_adapters.base import BaseOAuthProvider


class MetaAdsOAuthProvider(BaseOAuthProvider):
    provider = "META_ADS"
    name = "Meta Ads"
    auth_url_base = "https://www.facebook.com/v19.0/dialog/oauth"
    token_url = "https://graph.facebook.com/v19.0/oauth/access_token"
    default_scopes = ["ads_management", "ads_read"]

    def __init__(self):
        client_id = os.getenv("META_APP_ID") or os.getenv("META_CLIENT_ID", "")
        client_secret = os.getenv("META_APP_SECRET") or os.getenv("META_CLIENT_SECRET", "")
        super().__init__(client_id=client_id, client_secret=client_secret)

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        if self.is_configured:
            def _get():
                return requests.get(
                    self.token_url,
                    params={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                    timeout=15.0,
                )
            resp = await asyncio.to_thread(_get)
            resp.raise_for_status()
            data = resp.json()
            expires_in = data.get("expires_in", 5184000)
            return {
                "access_token": data["access_token"],
                "refresh_token": None,
                "token_type": data.get("token_type", "Bearer"),
                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                "scopes": self.default_scopes,
            }
        else:
            expires_at = datetime.now(timezone.utc) + timedelta(days=60)
            return {
                "access_token": f"EAAB_meta_ads_{uuid.uuid4().hex[:22]}",
                "refresh_token": None,
                "token_type": "Bearer",
                "expires_at": expires_at,
                "scopes": self.default_scopes,
            }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "act_99120488",
                "account_name": "Othership Coworking UK & EMEA Meta Pixel & Ads",
                "currency": "GBP",
            }
        ]

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        expires_at = datetime.now(timezone.utc) + timedelta(days=60)
        return {
            "access_token": f"EAAB_meta_refreshed_{uuid.uuid4().hex[:22]}",
            "expires_at": expires_at,
        }
