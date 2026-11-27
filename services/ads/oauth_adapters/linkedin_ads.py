"""Live, read-only LinkedIn Ads OAuth and account discovery."""
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from services.ads.oauth_adapters.base import BaseOAuthProvider, OAuthProviderNotReady


class LinkedInAdsApiError(Exception):
    """A sanitized LinkedIn error suitable for displaying in the portal."""


class LinkedInAdsOAuthProvider(BaseOAuthProvider):
    provider = "LINKEDIN_ADS"
    name = "LinkedIn Ads"
    auth_url_base = "https://www.linkedin.com/oauth/v2/authorization"
    token_url = "https://www.linkedin.com/oauth/v2/accessToken"
    default_scopes = ["r_ads", "r_ads_reporting"]

    def __init__(self):
        client_id = os.getenv("LINKEDIN_ADS_CLIENT_ID") or os.getenv("LINKEDIN_CLIENT_ID", "")
        client_secret = os.getenv("LINKEDIN_ADS_CLIENT_SECRET") or os.getenv("LINKEDIN_CLIENT_SECRET", "")
        super().__init__(client_id=client_id.strip(), client_secret=client_secret.strip())

    def ensure_ready(self, instant: bool = False) -> None:
        if not self.is_configured:
            raise OAuthProviderNotReady("Configure the LinkedIn Ads client ID and secret on the backend first.")
        if instant:
            raise OAuthProviderNotReady("LinkedIn Ads requires sign-in with LinkedIn. Use Live OAuth to connect.")

    def connection_metadata(self) -> dict[str, Any]:
        return {"mode": "live", "auth_method": "oauth"}

    def get_authorization_url(self, state: str, redirect_uri: str, extra_params=None) -> str:
        self.ensure_ready()
        return super().get_authorization_url(state, redirect_uri, extra_params)

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(method, url, **kwargs)
            if response.status_code >= 400:
                messages = {
                    400: "LinkedIn rejected the request. Check the app scopes and registered redirect URI, then reconnect.",
                    401: "LinkedIn sign-in expired or was rejected. Connect LinkedIn Ads again.",
                    403: "LinkedIn denied access. The app needs Advertising API access and your account needs ad-account permission.",
                    429: "LinkedIn is limiting requests. Try again later.",
                }
                raise LinkedInAdsApiError(messages.get(response.status_code, "LinkedIn could not complete the request. Try again later."))
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            return data
        except (httpx.HTTPError, ValueError) as exc:
            raise LinkedInAdsApiError("Couldn't read LinkedIn's response. Try connecting again.") from exc

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        self.ensure_ready()
        data = await self._request("POST", self.token_url, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": self.client_id, "client_secret": self.client_secret,
        })
        return self._token_info(data)

    def _token_info(self, data: dict) -> dict[str, Any]:
        if not data.get("access_token"):
            raise LinkedInAdsApiError("LinkedIn did not return an access token. Connect again.")
        scopes = data.get("scope", " ".join(self.default_scopes)).replace(",", " ").split()
        if not set(self.default_scopes).issubset(scopes):
            raise LinkedInAdsApiError("LinkedIn did not grant the required advertising permissions. Check the app's Advertising API access.")
        return {
            "access_token": data["access_token"], "refresh_token": data.get("refresh_token"),
            "token_type": "Bearer", "scopes": scopes, "expires_in": int(data.get("expires_in", 5184000)),
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 5184000))),
        }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        self.ensure_ready()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "LinkedIn-Version": os.getenv("LINKEDIN_ADS_API_VERSION", "202610"),
            "X-Restli-Protocol-Version": "2.0.0",
        }
        params = {"q": "search", "pageSize": "1000"}
        accounts, seen_pages = {}, set()
        for _ in range(50):
            data = await self._request("GET", "https://api.linkedin.com/rest/adAccounts", headers=headers, params=params)
            for account in data.get("elements", []):
                account_id = str(account.get("id", ""))
                if account_id.isdigit():
                    accounts[account_id] = {
                        "account_id": account_id, "account_name": account.get("name") or f"LinkedIn Ads {account_id}",
                        "currency": account.get("currency"), "status": account.get("status"),
                    }
            next_page = data.get("metadata", {}).get("nextPageToken")
            if not next_page:
                return list(accounts.values())
            if next_page in seen_pages:
                break
            seen_pages.add(next_page)
            params["pageToken"] = next_page
        raise LinkedInAdsApiError("LinkedIn returned too many account pages. Please reconnect or contact support.")

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        self.ensure_ready()
        data = await self._request("POST", self.token_url, data={
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": self.client_id, "client_secret": self.client_secret,
        })
        return self._token_info(data)
