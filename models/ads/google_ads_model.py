"""Google Ads sync: run summaries and status."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

GoogleAdsMode = Literal["mock", "live"]


class GoogleAdsAccountRun(BaseModel):
    """One ad account read in a run."""

    account_id: str
    account_name: str | None = None
    connection_id: str
    campaigns: int = 0
    # Weekly results read from Google for this account's campaigns
    results: int = 0
    # Why the account couldn't be read (the other accounts still synced)
    error: str | None = None


class GoogleAdsSyncSummary(BaseModel):
    # "mock": generated data for the mock connections. "live": the Google Ads API.
    mode: GoogleAdsMode
    date_from: str
    date_to: str
    connections: int = 0
    accounts: int = 0
    accounts_failed: int = 0
    campaigns_created: int = 0
    campaigns_updated: int = 0
    campaigns_unchanged: int = 0
    # Campaigns with no portal campaign that weren't added (GOOGLE_ADS_CREATE_CAMPAIGNS is off, or a removed campaign
    # with no results in the window)
    campaigns_skipped: int = 0
    # Weekly results added, changed, unchanged and removed (weeks Google no longer reports)
    results_created: int = 0
    results_updated: int = 0
    results_unchanged: int = 0
    results_removed: int = 0
    # Reconciled cost lines left as they were
    spend_conflicts: int = 0
    account_runs: list[GoogleAdsAccountRun] = []
    # Plain notes, e.g. a linked campaign in another currency than its account
    warnings: list[str] = []


class GoogleAdsConnectedAccount(BaseModel):
    connection_id: str
    account_id: str
    account_name: str | None = None
    currency: str | None = None
    is_manager: bool = False
    company_name: str | None = None


class GoogleAdsSyncStatus(BaseModel):
    mode: GoogleAdsMode
    # The scheduled sync is on (GOOGLE_ADS_SYNC_ENABLED)
    enabled: bool
    # Connecting can work: always in mock mode; in live mode when the OAuth client id and secret are set
    configured: bool
    interval_minutes: int
    lookback_days: int
    # Connected accounts the sync reads in this mode (manager accounts are listed but not synced)
    connected_accounts: list[GoogleAdsConnectedAccount] = []
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    last_run_summary: GoogleAdsSyncSummary | None = None
