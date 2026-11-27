"""Bind LinkedIn consent to the initiating portal session and user."""
import os
import secrets
import time

from services.ads.oauth_adapters.base import OAuthProviderNotReady

SESSION_KEY = "linkedin_ads_oauth"
MAX_AGE = 600


def validate_redirect(redirect_uri: str) -> None:
    expected = os.getenv("LINKEDIN_ADS_REDIRECT_URI", "http://localhost:5180/oauth/callback").strip()
    if redirect_uri != expected:
        raise OAuthProviderNotReady("LinkedIn's redirect URI must match LINKEDIN_ADS_REDIRECT_URI and the LinkedIn app settings.")


def remember_state(session: dict, state: str, redirect_uri: str, user_id) -> None:
    validate_redirect(redirect_uri)
    session[SESSION_KEY] = {"state": state, "redirect_uri": redirect_uri, "user_id": str(user_id), "created": time.time()}


def consume_state(session: dict, state: str, redirect_uri: str, user_id) -> None:
    validate_redirect(redirect_uri)
    expected = session.get(SESSION_KEY) or {}
    if (not secrets.compare_digest(str(expected.get("state", "")), state)
            or expected.get("redirect_uri") != redirect_uri or expected.get("user_id") != str(user_id)
            or not 0 <= time.time() - expected.get("created", 0) <= MAX_AGE):
        raise OAuthProviderNotReady("LinkedIn authorization expired or didn't match this session. Start LinkedIn sign-in again.")
    session.pop(SESSION_KEY, None)
