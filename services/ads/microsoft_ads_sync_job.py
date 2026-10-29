"""Keeps Microsoft Advertising (Bing Ads) campaigns and their weekly results up to date from the connected accounts.

Runs as a background asyncio task started from server.py's lifespan when MICROSOFT_ADS_SYNC_ENABLED is on (off by
default), every MICROSOFT_ADS_SYNC_INTERVAL_MINUTES (default 360). It can also be run on demand from
POST /api/integrations/microsoft-ads/sync.

It works exactly like the Google Ads sync (the run itself is shared: services/ads/ad_platform_sync.py):
- Connections: every CONNECTED Microsoft Ads connection made in the current MICROSOFT_ADS_MODE
  (services/microsoft_ads_client.connection_mode).
- Reading: each account's campaigns and their weekly metrics (Monday to Sunday) for the last
  MICROSOFT_ADS_LOOKBACK_DAYS days (default 90). Mock mode generates them (services/ads/microsoft_ads_mock_data.py); live
  mode uses the Microsoft Advertising API. Microsoft statuses are read as Active -> Enabled, the paused and suspended
  ones -> Paused, Deleted -> Removed.
- Campaigns: linked through initiative_links (system MICROSOFT_ADS, external_id = the campaign id). Without one, and
  when MICROSOFT_ADS_CREATE_CAMPAIGNS is on (default), a campaign is added: Paid Search / Bing Ads, named as in
  Microsoft Advertising, brand of the connection's company (else TBD/All), with Microsoft's dates and budget,
  objective Awareness for Audience campaigns and Leads otherwise.
- Results: one ad result per campaign per week, source MICROSOFT_ADS_API, mirrored into Microsoft Advertising spend
  lines. HubSpot figures stay; weeks no longer reported go.
- The run state is kept in integration_sync_state "microsoft_ads" (live) or "microsoft_ads_mock" (mock).
"""

import datetime
import uuid
from typing import Any, Dict, List, Optional

from services.ads import ad_platform_sync as shared
from services.ads import oauth_connection_service as oauth_svc
from services.ads.microsoft_ads_client import (
    MOCK_MODE,
    MicrosoftAdsApiError,
    connection_mode,
    fetch_campaigns,
    microsoft_ads_mode,
)
from services.ads.microsoft_ads_mock_data import mock_account_campaigns

PROVIDER = "MICROSOFT_ADS"
SYNC_NAME = "microsoft_ads"
# Mock mode keeps its own run state, so a mock run never changes what the live sync remembers
MOCK_SYNC_NAME = "microsoft_ads_mock"
RESULT_SOURCE = "MICROSOFT_ADS_API"
LINK_SYSTEM = "MICROSOFT_ADS"
SUB_CHANNEL = "BING_ADS"
DEFAULT_LOOKBACK_DAYS = 90
MAX_LOOKBACK_DAYS = 730
DEFAULT_SYNC_INTERVAL_MINUTES = 360
MIN_SYNC_INTERVAL_MINUTES = 5
ALREADY_RUNNING_MESSAGE = "A Microsoft Ads sync is already running. Try again in a minute."
NEEDS_SIGN_IN_MESSAGE = "The Microsoft Ads connection needs to be signed in again on OAuth Integrations."
# Campaign types that are about being seen rather than leads
AWARENESS_CHANNEL_TYPES = frozenset({"AUDIENCE"})


class MicrosoftAdsSyncAlreadyRunning(shared.AdSyncAlreadyRunning):
    """Another Microsoft Ads sync is running (here or on another server). The message is safe to show to users."""

    def __init__(self, message: str = ALREADY_RUNNING_MESSAGE):
        super().__init__(message)


class MicrosoftAdsSyncFailed(shared.AdSyncFailed):
    """No Microsoft Ads account could be read. The message is safe to show to users."""


# --- Settings ---

def microsoft_ads_sync_enabled() -> bool:
    return shared.flag("MICROSOFT_ADS_SYNC_ENABLED", False)


def microsoft_ads_create_campaigns() -> bool:
    return shared.flag("MICROSOFT_ADS_CREATE_CAMPAIGNS", True)


def lookback_days() -> int:
    return shared.int_setting("MICROSOFT_ADS_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS, 7, MAX_LOOKBACK_DAYS)


def sync_interval_minutes() -> int:
    return shared.int_setting("MICROSOFT_ADS_SYNC_INTERVAL_MINUTES", DEFAULT_SYNC_INTERVAL_MINUTES, MIN_SYNC_INTERVAL_MINUTES, 10_080)


def sync_name() -> str:
    return MOCK_SYNC_NAME if microsoft_ads_mode() == MOCK_MODE else SYNC_NAME


def read_window(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    """From the Monday of the week MICROSOFT_ADS_LOOKBACK_DAYS ago to today."""
    return shared.read_window(today, lookback_days())


# --- Connections and reading ---

async def usable_connections(conn, mode: str, connection_ids: Optional[List[uuid.UUID]] = None) -> List[Dict[str, Any]]:
    """CONNECTED Microsoft Ads connections made in `mode`, a company's own first, with their discovered accounts."""
    return await shared.fetch_usable_connections(conn, PROVIDER, connection_mode, mode, connection_ids)


async def _read_account(mode: str, connection: Dict[str, Any], account: Dict[str, Any], date_from: datetime.date,
                        date_to: datetime.date, tokens: Dict[Any, Optional[str]]) -> List[Dict[str, Any]]:
    if mode == MOCK_MODE:
        return mock_account_campaigns(account, date_from, date_to)
    if connection["id"] not in tokens:
        tokens[connection["id"]] = await oauth_svc.get_valid_access_token(connection["id"])
    token = tokens[connection["id"]]
    if not token:
        raise MicrosoftAdsApiError(NEEDS_SIGN_IN_MESSAGE)
    return await fetch_campaigns(token, account, date_from, date_to)


PLATFORM = shared.AdPlatform(
    label="Microsoft Ads", provider=PROVIDER, sync_name=SYNC_NAME, mock_sync_name=MOCK_SYNC_NAME,
    result_source=RESULT_SOURCE, link_system=LINK_SYSTEM, sub_channel=SUB_CHANNEL,
    audit_campaign_key="microsoft_ads_campaign_id", event_prefix="microsoft_ads",
    awareness_channel_types=AWARENESS_CHANNEL_TYPES,
    mode=microsoft_ads_mode, create_campaigns=lambda: microsoft_ads_create_campaigns(), lookback_days=lambda: lookback_days(),
    # Through this module's names, so tests can limit a run to their own connections
    usable_connections=lambda conn, mode, ids=None: usable_connections(conn, mode, ids),
    read_account=lambda *args: _read_account(*args),
    api_error=MicrosoftAdsApiError, already_running=MicrosoftAdsSyncAlreadyRunning, failed=MicrosoftAdsSyncFailed,
)


# --- Running ---

async def sync_microsoft_ads(connection_ids: Optional[List[uuid.UUID]] = None,
                             today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Run one sync (every usable connection, or only these) and return its summary.

    Raises MicrosoftAdsSyncAlreadyRunning, or MicrosoftAdsSyncFailed when no account could be read (stored as a
    failure).
    """
    return await shared.run(PLATFORM, connection_ids, today)


async def get_microsoft_ads_sync_state() -> Optional[Dict[str, Any]]:
    """The stored state of the Microsoft Ads sync in the current mode, or None before its first run."""
    return await shared.get_sync_state(PLATFORM)


async def connected_accounts() -> List[Dict[str, Any]]:
    """The accounts of the usable connections in the current mode."""
    return await shared.connected_accounts(PLATFORM)


async def microsoft_ads_sync_loop(interval_seconds: Optional[int] = None) -> None:
    """Run sync_microsoft_ads() forever on a fixed interval; a failed run is logged and retried next interval."""
    await shared.sync_loop(PLATFORM, sync_microsoft_ads, interval_seconds or sync_interval_minutes() * 60)
