"""Keyless Google marketing reader authentication, loaded only when requested.

Local runs use the caller's Application Default Credentials (ADC). Credentials
and temporary tokens stay in this process; callers must not log or persist them.
"""

import asyncio
import os
import threading
from functools import partial
from typing import Literal

import google.auth
import requests
from google.auth import impersonated_credentials
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request

ReaderSource = Literal["ga4", "gsc", "ads"]
DEFAULT_READER = "automations-reader@zt-marketing-ops.iam.gserviceaccount.com"
_SCOPES = {
    "ga4": "https://www.googleapis.com/auth/analytics.readonly",
    "gsc": "https://www.googleapis.com/auth/webmasters.readonly",
    "ads": "https://www.googleapis.com/auth/adwords",
}
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_credentials: dict[tuple[str, str], impersonated_credentials.Credentials] = {}
_lock = threading.Lock()


class GoogleReaderAuthError(RuntimeError):
    """Reader authentication failed; the message contains no credential details."""


def _get_access_token(source: ReaderSource) -> str:
    """Reuse valid credentials and serialize refreshes across concurrent callers."""
    if source not in _SCOPES:
        raise ValueError("Google reader source must be ga4, gsc or ads")

    target = os.getenv("GOOGLE_READER_SERVICE_ACCOUNT", "").strip() or DEFAULT_READER
    key = (target, _SCOPES[source])
    with _lock:
        try:
            credentials = _credentials.get(key)
            if credentials is None:
                me, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
                credentials = impersonated_credentials.Credentials(
                    source_credentials=me,
                    target_principal=target,
                    target_scopes=[_SCOPES[source]],
                    lifetime=3600,
                )
                _credentials[key] = credentials

            if not credentials.valid:
                with requests.Session() as session:
                    request = partial(Request(session=session), timeout=30)
                    credentials.refresh(request)
            if not credentials.token:
                raise GoogleReaderAuthError("Google reader returned no access token")
            return credentials.token
        except (GoogleAuthError, requests.RequestException, OSError):
            _credentials.pop(key, None)
            # Provider exception messages can include credential or response data.
            raise GoogleReaderAuthError(
                "Google reader authentication failed. Check your Application "
                "Default Credentials and Service Account Token Creator access "
                "to the reader. For local sign-in, run "
                "gcloud auth application-default login with your company account."
            ) from None


async def get_reader_access_token(source: ReaderSource) -> str:
    """Return a source-scoped reader token, refreshing off FastAPI's event loop.

Only ga4, gsc and ads are accepted. The reader's account permissions enforce
read-only Ads access because Google's adwords OAuth scope is not read-only.
"""
    return await asyncio.to_thread(_get_access_token, source)
