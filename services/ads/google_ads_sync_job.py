"""Keeps Google Ads campaigns and their weekly results up to date from the connected Google Ads accounts.

Runs as a background asyncio task started from server.py's lifespan when GOOGLE_ADS_SYNC_ENABLED is on (off by
default), every GOOGLE_ADS_SYNC_INTERVAL_MINUTES (default 360), like services/navan/navan_cost_sync_job.py. It can also be
run on demand from POST /api/integrations/google-ads/sync.

How a run works:
- Connections: every CONNECTED Google Ads connection (oauth_connections) made in the current GOOGLE_ADS_MODE. Mock
  connections are only read in mock mode and real ones only in live mode (services/google_ads_client.connection_mode).
  Each non-manager account in a connection's discovered accounts is read once, even when two connections list it
  (a company's own connection wins over the organization-wide one, for the brand).
- Reading, with no database connection held: each account's campaigns (id, name, status, type, dates, budget) and its
  metrics per week (cost, impressions, clicks, conversions) for the last GOOGLE_ADS_LOOKBACK_DAYS days (default 90,
  from the Monday of that week to today). Conversions change after the fact, so the whole window is read every run.
  Mock mode generates them (services/ads/google_ads_mock_data.py); live mode uses the Google Ads API. An account that
  can't be read is noted in the summary and the others still sync; when no account can be read the run fails.
- Campaigns: a Google campaign is the portal campaign linked to it (initiative_links, system GOOGLE_ADS, external_id
  = the campaign id). Without one, and when GOOGLE_ADS_CREATE_CAMPAIGNS is on (default), a campaign is added: Paid
  Search / Google Ads, named as in Google, brand of the connection's company (else TBD/All), in the account's
  currency, with Google's dates and budget (for one without an end date, the budget so far: see campaign_budget), objective Awareness for display and video campaigns and Leads otherwise,
  and linked to it with the account id. A removed campaign with nothing in the window isn't added.
  Linked campaigns follow Google on every run: status (Enabled -> Active, or Planned before its start and Completed
  after its end; Paused -> Paused; Removed -> Cancelled), dates and budget. The name only follows Google for
  campaigns the sync added (nobody is recorded as creating them); a name someone gave a campaign stays.
- Results: one ad result per campaign per week (no ad group, Monday to Sunday), source GOOGLE_ADS_API, created or
  updated in place by services/ads_service.apply_results, so re-runs never duplicate. Budget per week is 7 days of the
  daily budget. HubSpot contacts, MQLs and SQLs are never sent, so the values already on a result stay. A week inside
  the window that Google no longer reports loses its synced result (results from other sources stay). A linked
  campaign in another currency than its account gets no results (a warning in the summary).
- Spend and outcomes: services/ads_service.sync_initiatives mirrors each result's spend into a Google Ads spend line
  and the results into the campaign's ADS outcomes, as for any other ad result.
- Every write, its audit entries (no actor: Google Ads made the change) and the run summary are saved in one
  transaction, under an advisory lock, so two servers never sync at the same time. The run state is kept in
  integration_sync_state "google_ads" (live) or "google_ads_mock" (mock).

The run itself is shared with the Microsoft Ads sync (services/ads/ad_platform_sync.py); this module says what is
particular to Google Ads.
"""

import datetime
import uuid
from typing import Any, Dict, List, Optional

from services.ads import ad_platform_sync as shared
from services.ads import oauth_connection_service as oauth_svc
from services.ads.ad_platform_sync import campaign_budget as campaign_budget
from services.ads.ad_platform_sync import initiative_status as initiative_status
from services.ads.ad_platform_sync import result_status as result_status
from services.ads.google_ads_client import (
    MOCK_MODE,
    GoogleAdsApiError,
    connection_mode,
    fetch_campaigns,
    google_ads_mode,
)
from services.ads.google_ads_mock_data import mock_account_campaigns

PROVIDER = "GOOGLE_ADS"
SYNC_NAME = "google_ads"
# Mock mode keeps its own run state, so a mock run never changes what the live sync remembers
MOCK_SYNC_NAME = "google_ads_mock"
RESULT_SOURCE = "GOOGLE_ADS_API"
LINK_SYSTEM = "GOOGLE_ADS"
SUB_CHANNEL = "GOOGLE_ADS"
DEFAULT_LOOKBACK_DAYS = 90
MAX_LOOKBACK_DAYS = 730
DEFAULT_SYNC_INTERVAL_MINUTES = 360
MIN_SYNC_INTERVAL_MINUTES = 5
ALREADY_RUNNING_MESSAGE = "A Google Ads sync is already running. Try again in a minute."
NEEDS_SIGN_IN_MESSAGE = "The Google Ads connection needs to be signed in again on OAuth Integrations."
# Campaign types that are about being seen rather than leads
AWARENESS_CHANNEL_TYPES = {"DISPLAY", "VIDEO", "DEMAND_GEN", "DISCOVERY"}


class GoogleAdsSyncAlreadyRunning(shared.AdSyncAlreadyRunning):
    """Another Google Ads sync is running (here or on another server). The message is safe to show to users."""

    def __init__(self, message: str = ALREADY_RUNNING_MESSAGE):
        super().__init__(message)


class GoogleAdsSyncFailed(shared.AdSyncFailed):
    """No Google Ads account could be read. The message is safe to show to users."""


# --- Settings ---

def google_ads_sync_enabled() -> bool:
    return shared.flag("GOOGLE_ADS_SYNC_ENABLED", False)


def google_ads_create_campaigns() -> bool:
    return shared.flag("GOOGLE_ADS_CREATE_CAMPAIGNS", True)


def lookback_days() -> int:
    return shared.int_setting("GOOGLE_ADS_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS, 7, MAX_LOOKBACK_DAYS)


def sync_interval_minutes() -> int:
    return shared.int_setting("GOOGLE_ADS_SYNC_INTERVAL_MINUTES", DEFAULT_SYNC_INTERVAL_MINUTES, MIN_SYNC_INTERVAL_MINUTES, 10_080)


def sync_name() -> str:
    return MOCK_SYNC_NAME if google_ads_mode() == MOCK_MODE else SYNC_NAME


# --- Rules ---

def read_window(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    """From the Monday of the week GOOGLE_ADS_LOOKBACK_DAYS ago to today, so the first week is a whole week."""
    return shared.read_window(today, lookback_days())


def objective_for(channel_type: str) -> str:
    return "AWARENESS" if channel_type in AWARENESS_CHANNEL_TYPES else "LEADS"


# --- Connections and reading ---

async def usable_connections(conn, mode: str, connection_ids: Optional[List[uuid.UUID]] = None) -> List[Dict[str, Any]]:
    """CONNECTED Google Ads connections made in `mode`, a company's own first, with their discovered accounts."""
    return await shared.fetch_usable_connections(conn, PROVIDER, connection_mode, mode, connection_ids)


async def _read_account(mode: str, connection: Dict[str, Any], account: Dict[str, Any], date_from: datetime.date,
                        date_to: datetime.date, tokens: Dict[Any, Optional[str]]) -> List[Dict[str, Any]]:
    if mode == MOCK_MODE:
        return mock_account_campaigns(account, date_from, date_to)
    if connection["id"] not in tokens:
        tokens[connection["id"]] = await oauth_svc.get_valid_access_token(connection["id"])
    token = tokens[connection["id"]]
    if not token:
        raise GoogleAdsApiError(NEEDS_SIGN_IN_MESSAGE)
    return await fetch_campaigns(token, account, date_from, date_to)


PLATFORM = shared.AdPlatform(
    label="Google Ads", provider=PROVIDER, sync_name=SYNC_NAME, mock_sync_name=MOCK_SYNC_NAME,
    result_source=RESULT_SOURCE, link_system=LINK_SYSTEM, sub_channel=SUB_CHANNEL,
    audit_campaign_key="google_ads_campaign_id", event_prefix="google_ads",
    awareness_channel_types=frozenset(AWARENESS_CHANNEL_TYPES),
    mode=google_ads_mode, create_campaigns=lambda: google_ads_create_campaigns(), lookback_days=lambda: lookback_days(),
    # Through this module's names, so tests can limit a run to their own connections
    usable_connections=lambda conn, mode, ids=None: usable_connections(conn, mode, ids),
    read_account=lambda *args: _read_account(*args),
    api_error=GoogleAdsApiError, already_running=GoogleAdsSyncAlreadyRunning, failed=GoogleAdsSyncFailed,
)


# --- Running ---

async def sync_google_ads(connection_ids: Optional[List[uuid.UUID]] = None,
                          today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Run one sync (every usable connection, or only these) and return its summary.

    Raises GoogleAdsSyncAlreadyRunning, or GoogleAdsSyncFailed when no account could be read (stored as a failure).
    """
    return await shared.run(PLATFORM, connection_ids, today)


async def get_google_ads_sync_state() -> Optional[Dict[str, Any]]:
    """The stored state of the Google Ads sync in the current mode, or None before its first run."""
    return await shared.get_sync_state(PLATFORM)


async def connected_accounts() -> List[Dict[str, Any]]:
    """The accounts of the usable connections in the current mode (managers included, flagged)."""
    return await shared.connected_accounts(PLATFORM)


async def google_ads_sync_loop(interval_seconds: Optional[int] = None) -> None:
    """Run sync_google_ads() forever on a fixed interval; a failed run is logged and retried next interval."""
    await shared.sync_loop(PLATFORM, sync_google_ads, interval_seconds or sync_interval_minutes() * 60)


