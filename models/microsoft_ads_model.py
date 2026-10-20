"""Microsoft Ads sync: run summaries and status (the same shape as the Google Ads sync's)."""
from models.google_ads_model import (
    GoogleAdsAccountRun,
    GoogleAdsConnectedAccount,
    GoogleAdsSyncStatus,
    GoogleAdsSyncSummary,
)


class MicrosoftAdsAccountRun(GoogleAdsAccountRun):
    """One ad account read in a run."""


class MicrosoftAdsSyncSummary(GoogleAdsSyncSummary):
    """mode "mock": generated data for the mock connections. "live": the Microsoft Advertising API."""

    account_runs: list[MicrosoftAdsAccountRun] = []


class MicrosoftAdsConnectedAccount(GoogleAdsConnectedAccount):
    pass


class MicrosoftAdsSyncStatus(GoogleAdsSyncStatus):
    # Connected accounts the sync reads in this mode
    connected_accounts: list[MicrosoftAdsConnectedAccount] = []
    last_run_summary: MicrosoftAdsSyncSummary | None = None
