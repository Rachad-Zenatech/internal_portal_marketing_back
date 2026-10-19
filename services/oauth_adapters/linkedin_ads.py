"""LinkedIn Ads OAuth 2.0 Provider Adapter"""
import os
import uuid
import asyncio
import requests
from datetime import datetime, timedelta, timezone
from typing import Any

from services.oauth_adapters.base import BaseOAuthProvider


class LinkedInAdsOAuthProvider(BaseOAuthProvider):
    provider = "LINKEDIN_ADS"
    name = "LinkedIn Ads"
    auth_url_base = "https://www.linkedin.com/oauth/v2/authorization"
    token_url = "https://www.linkedin.com/oauth/v2/accessToken"
    default_scopes = ["r_ads", "r_ads_reporting"]

    def __init__(self):
        client_id = os.getenv("LINKEDIN_ADS_CLIENT_ID") or os.getenv("LINKEDIN_CLIENT_ID", "")
        client_secret = os.getenv("LINKEDIN_ADS_CLIENT_SECRET") or os.getenv("LINKEDIN_CLIENT_SECRET", "")
        super().__init__(client_id=client_id, client_secret=client_secret)

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        if self.is_configured:
            def _post():
                return requests.post(
                    self.token_url,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    timeout=15.0,
                )
            resp = await asyncio.to_thread(_post)
            resp.raise_for_status()
            data = resp.json()
            expires_in = data.get("expires_in", 5184000)
            refresh_token = data.get("refresh_token")
            return {
                "access_token": data["access_token"],
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                "scopes": self.default_scopes,
            }
        else:
            expires_at = datetime.now(timezone.utc) + timedelta(days=60)
            return {
                "access_token": f"AQV_li_ads_{uuid.uuid4().hex[:20]}",
                "refresh_token": f"AQV_li_refresh_{uuid.uuid4().hex[:20]}",
                "token_type": "Bearer",
                "expires_at": expires_at,
                "scopes": self.default_scopes,
            }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "501234567",
                "account_name": "DeskFlex Enterprise B2B Sponsored Content",
                "currency": "USD",
            }
        ]

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        expires_at = datetime.now(timezone.utc) + timedelta(days=60)
        return {
            "access_token": f"AQV_li_refreshed_{uuid.uuid4().hex[:20]}",
            "expires_at": expires_at,
        }
