"""Organic Social Media OAuth 2.0 Provider Adapters"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from services.oauth_adapters.base import BaseOAuthProvider


class LinkedInPageOAuthProvider(BaseOAuthProvider):
    provider = "LINKEDIN_PAGE"
    name = "LinkedIn Company Page"
    auth_url_base = "https://www.linkedin.com/oauth/v2/authorization"
    token_url = "https://www.linkedin.com/oauth/v2/accessToken"
    default_scopes = ["w_organization_social", "r_organization_social"]

    def __init__(self):
        super().__init__(
            client_id=os.getenv("LINKEDIN_CLIENT_ID", ""),
            client_secret=os.getenv("LINKEDIN_CLIENT_SECRET", ""),
        )

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        return {
            "access_token": f"AQV_li_page_{uuid.uuid4().hex[:20]}",
            "refresh_token": f"AQV_li_page_ref_{uuid.uuid4().hex[:20]}",
            "token_type": "Bearer",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
            "scopes": self.default_scopes,
        }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "urn:li:organization:918231",
                "account_name": "ZenaTech Group Official Page",
            }
        ]

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        return {
            "access_token": f"AQV_li_page_refreshed_{uuid.uuid4().hex[:20]}",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
        }


class XTwitterOAuthProvider(BaseOAuthProvider):
    provider = "X_TWITTER"
    name = "X (Twitter)"
    auth_url_base = "https://twitter.com/i/oauth2/authorize"
    token_url = "https://api.twitter.com/2/oauth2/token"
    default_scopes = ["tweet.read", "tweet.write", "users.read", "offline.access"]

    def __init__(self):
        super().__init__(
            client_id=os.getenv("TWITTER_CLIENT_ID", ""),
            client_secret=os.getenv("TWITTER_CLIENT_SECRET", ""),
        )

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        return {
            "access_token": f"xt_tok_{uuid.uuid4().hex[:24]}",
            "refresh_token": f"xt_ref_{uuid.uuid4().hex[:24]}",
            "token_type": "Bearer",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
            "scopes": self.default_scopes,
        }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "@DeskFlexUSA",
                "account_name": "DeskFlex Workspace Solutions",
            }
        ]

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        return {
            "access_token": f"xt_tok_refreshed_{uuid.uuid4().hex[:24]}",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
        }


class InstagramOAuthProvider(BaseOAuthProvider):
    provider = "INSTAGRAM"
    name = "Instagram Business"
    auth_url_base = "https://www.facebook.com/v19.0/dialog/oauth"
    token_url = "https://graph.facebook.com/v19.0/oauth/access_token"
    default_scopes = ["instagram_basic", "instagram_content_publish", "pages_show_list"]

    def __init__(self):
        super().__init__(
            client_id=os.getenv("META_APP_ID", ""),
            client_secret=os.getenv("META_APP_SECRET", ""),
        )

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        return {
            "access_token": f"IGQVJ_{uuid.uuid4().hex[:24]}",
            "refresh_token": None,
            "token_type": "Bearer",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
            "scopes": self.default_scopes,
        }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "@othership.work",
                "account_name": "Othership Flexible Coworking",
            }
        ]

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        return {
            "access_token": f"IGQVJ_refreshed_{uuid.uuid4().hex[:24]}",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
        }


class FacebookPageOAuthProvider(BaseOAuthProvider):
    provider = "FACEBOOK_PAGE"
    name = "Facebook Page"
    auth_url_base = "https://www.facebook.com/v19.0/dialog/oauth"
    token_url = "https://graph.facebook.com/v19.0/oauth/access_token"
    default_scopes = ["pages_manage_posts", "pages_read_engagement"]

    def __init__(self):
        super().__init__(
            client_id=os.getenv("META_APP_ID", ""),
            client_secret=os.getenv("META_APP_SECRET", ""),
        )

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        return {
            "access_token": f"EAAFB_{uuid.uuid4().hex[:24]}",
            "refresh_token": None,
            "token_type": "Bearer",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
            "scopes": self.default_scopes,
        }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "109283746501928",
                "account_name": "ZenaTech Enterprise Page",
            }
        ]

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        return {
            "access_token": f"EAAFB_refreshed_{uuid.uuid4().hex[:24]}",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
        }


class YouTubeOAuthProvider(BaseOAuthProvider):
    provider = "YOUTUBE"
    name = "YouTube Channel"
    auth_url_base = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    default_scopes = ["https://www.googleapis.com/auth/youtube.readonly"]

    def __init__(self):
        super().__init__(
            client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        )

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        return {
            "access_token": f"ya29.yt_{uuid.uuid4().hex[:22]}",
            "refresh_token": f"1//yt_ref_{uuid.uuid4().hex[:22]}",
            "token_type": "Bearer",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=90),
            "scopes": self.default_scopes,
        }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "UC-ZENATECH-CORP-01",
                "account_name": "ZenaTech Global Tech & Innovations",
            }
        ]

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        return {
            "access_token": f"ya29.yt_refreshed_{uuid.uuid4().hex[:22]}",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=90),
        }


class TikTokOAuthProvider(BaseOAuthProvider):
    provider = "TIKTOK"
    name = "TikTok Business"
    auth_url_base = "https://www.tiktok.com/v2/auth/authorize/"
    token_url = "https://open.tiktokapis.com/v2/oauth/token/"
    default_scopes = ["user.info.basic", "video.list"]

    def __init__(self):
        super().__init__(
            client_id=os.getenv("TIKTOK_CLIENT_KEY", ""),
            client_secret=os.getenv("TIKTOK_CLIENT_SECRET", ""),
        )

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        return {
            "access_token": f"act.tt_{uuid.uuid4().hex[:22]}",
            "refresh_token": f"rft.tt_{uuid.uuid4().hex[:22]}",
            "token_type": "Bearer",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
            "scopes": self.default_scopes,
        }

    async def discover_accounts(self, access_token: str) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "@zenatech_official",
                "account_name": "ZenaTech Official TikTok",
            }
        ]

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        return {
            "access_token": f"act.tt_refreshed_{uuid.uuid4().hex[:22]}",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=60),
        }
