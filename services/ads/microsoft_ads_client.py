"""Microsoft Advertising (Bing Ads): the mode (mock or live), the app credentials, and the few API calls the portal makes.

Everything here reads only. The portal never changes anything in Microsoft Advertising.

Mode (MICROSOFT_ADS_MODE):
- "mock": no call to Microsoft at all. Connecting stores fake tokens and the mock accounts, and the sync generates
  campaigns and weekly results for them (services/ads/microsoft_ads_mock_data.py). The default outside production.
- "live": real Microsoft sign-in and the Microsoft Advertising API. The default when APP_ENV=production. Needs
  MICROSOFT_ADS_CLIENT_ID and MICROSOFT_ADS_CLIENT_SECRET (the Microsoft Advertising app registration, never the
  portal's sign-in app MICROSOFT_CLIENT_ID); without them connecting fails with NOT_SET_UP_MESSAGE. The API calls
  also need MICROSOFT_ADS_DEVELOPER_TOKEN.

Each connection remembers the mode it was made in (oauth_connections.connection_metadata "mode"), so mock connections
are never used as real ones and the other way round.

Calls (Bing Ads API v13, REST/JSON):
- Customer Management: User/Query (the signed-in user) and Accounts/Search (the ad accounts that user can open).
- Campaign Management: Campaigns/QueryByAccountId (each account's campaigns).
- Reporting: GenerateReport/Submit and /Poll for a campaign performance report per week (Monday to Sunday), then the
  zipped CSV it points to.
Headers: Authorization (the access token), DeveloperToken, and CustomerId / CustomerAccountId for the account read.
MICROSOFT_ADS_CUSTOMER_ID is used as CustomerId when an account doesn't say which customer it belongs to.
"""
import asyncio
import csv
import io
import logging
import os
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from services.ads import token_crypto_service as crypto

logger = logging.getLogger(__name__)

# The one place the API version is pinned
API_VERSION = "v13"
CUSTOMER_API = f"https://clientcenter.api.bingads.microsoft.com/CustomerManagement/{API_VERSION}"
CAMPAIGN_API = f"https://campaign.api.bingads.microsoft.com/CampaignManagement/{API_VERSION}"
REPORTING_API = f"https://reporting.api.bingads.microsoft.com/Reporting/{API_VERSION}"
HTTP_TIMEOUT_SECONDS = 60.0
# Waiting for a report: every REPORT_POLL_SECONDS, at most REPORT_POLL_ATTEMPTS times
REPORT_POLL_SECONDS = 3.0
REPORT_POLL_ATTEMPTS = 40

MOCK_MODE = "mock"
LIVE_MODE = "live"
NOT_SET_UP_MESSAGE = "Microsoft Ads isn't set up: add MICROSOFT_ADS_CLIENT_ID and MICROSOFT_ADS_CLIENT_SECRET"
NO_DEVELOPER_TOKEN_MESSAGE = "Microsoft Ads needs a developer token: add MICROSOFT_ADS_DEVELOPER_TOKEN"
# Fake access tokens start with this (mock mode, and connections made before the mode was recorded)
MOCK_TOKEN_PREFIX = "EwBoA_msft_"
# Every campaign type, so none is left out (the API returns only Search campaigns by default)
CAMPAIGN_TYPES = "Search Shopping DynamicSearchAds Audience Hotel PerformanceMax App"
# Microsoft's campaign statuses -> the shape the shared sync reads (services/ads/ad_platform_sync.py)
_STATUSES = {
    "active": "ENABLED", "paused": "PAUSED", "budgetpaused": "PAUSED", "budgetandmanualpaused": "PAUSED",
    "suspended": "PAUSED", "deleted": "REMOVED",
}
_TYPES = {
    "search": "SEARCH", "shopping": "SHOPPING", "dynamicsearchads": "DYNAMIC_SEARCH_ADS", "audience": "AUDIENCE",
    "hotel": "HOTEL", "performancemax": "PERFORMANCE_MAX", "app": "APP",
}


class MicrosoftAdsApiError(Exception):
    """Microsoft Advertising refused a call or couldn't be reached. The message is safe to show to users."""


def microsoft_ads_mode() -> str:
    """MICROSOFT_ADS_MODE when it's mock or live; otherwise live in production and mock everywhere else."""
    value = os.getenv("MICROSOFT_ADS_MODE", "").strip().lower()
    if value in (MOCK_MODE, LIVE_MODE):
        return value
    return LIVE_MODE if os.getenv("APP_ENV", "development").strip().lower() == "production" else MOCK_MODE


def microsoft_ads_credentials() -> tuple[str, str]:
    """The Microsoft Advertising app's own client id and secret. Never MICROSOFT_CLIENT_ID / _SECRET: those are the
    portal's sign-in (SSO) app."""
    return os.getenv("MICROSOFT_ADS_CLIENT_ID", "").strip(), os.getenv("MICROSOFT_ADS_CLIENT_SECRET", "").strip()


def microsoft_ads_configured() -> bool:
    """Whether connecting can work in the current mode (mock mode needs nothing)."""
    if microsoft_ads_mode() == MOCK_MODE:
        return True
    client_id, client_secret = microsoft_ads_credentials()
    return bool(client_id and client_secret)


def connection_mode(metadata: dict | None, access_token_encrypted: str | None) -> str:
    """The mode a Microsoft Ads connection was made in: as recorded, else mock when its token is a fake one."""
    recorded = (metadata or {}).get("mode")
    if recorded in (MOCK_MODE, LIVE_MODE):
        return recorded
    try:
        token = crypto.decrypt_token(access_token_encrypted) or ""
    except Exception:
        token = ""
    return MOCK_MODE if token.startswith(MOCK_TOKEN_PREFIX) else LIVE_MODE


def _http_client() -> httpx.AsyncClient:
    """Replaced in tests with a client on an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)


def _headers(access_token: str, customer_id: str | None = None, account_id: str | None = None) -> dict[str, str]:
    developer_token = os.getenv("MICROSOFT_ADS_DEVELOPER_TOKEN", "").strip()
    if not developer_token:
        raise MicrosoftAdsApiError(NO_DEVELOPER_TOKEN_MESSAGE)
    headers = {
        "Authorization": f"Bearer {access_token}", "DeveloperToken": developer_token,
        "Accept": "application/json", "Content-Type": "application/json",
    }
    customer = customer_id or os.getenv("MICROSOFT_ADS_CUSTOMER_ID", "").strip()
    if customer:
        headers["CustomerId"] = str(customer)
    if account_id:
        headers["CustomerAccountId"] = str(account_id)
    return headers


def _error_message(response: httpx.Response) -> str:
    """A short, plain reason from a Microsoft Advertising error response."""
    reason = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            errors = body.get("OperationErrors") or body.get("Errors") or body.get("BatchErrors") or []
            reason = next((e.get("Message") for e in errors if isinstance(e, dict) and e.get("Message")), "") or body.get("Message") or ""
    except ValueError:
        reason = ""
    if response.status_code == 401:
        return "Microsoft Ads didn't accept the sign-in. Connect Microsoft Ads again on OAuth Integrations."
    if response.status_code == 403:
        return f"Microsoft Ads refused access{': ' + reason if reason else ''}."
    if response.status_code == 429:
        return "Microsoft Ads is limiting requests right now. Try again later."
    return f"Microsoft Ads returned an error ({response.status_code}){': ' + reason if reason else ''}."


async def _post(url: str, access_token: str, body: dict, customer_id: str | None = None,
                account_id: str | None = None) -> dict:
    headers = _headers(access_token, customer_id, account_id)
    try:
        async with _http_client() as client:
            response = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        logger.warning(f"Microsoft Ads request failed: {type(exc).__name__}", extra={"event": "microsoft_ads_request_failed"})
        raise MicrosoftAdsApiError("Couldn't reach Microsoft Ads. Try again in a few minutes.") from exc
    if response.status_code >= 400:
        raise MicrosoftAdsApiError(_error_message(response))
    try:
        return response.json() or {}
    except ValueError as exc:
        raise MicrosoftAdsApiError("Microsoft Ads sent a reply the portal couldn't read.") from exc


# --- Accounts ---

def account_from_api(row: dict) -> dict:
    """One account as stored in connection_metadata.discovered_accounts, from an AdvertiserAccount."""
    account_id = str(row.get("Id") or "")
    account = {
        "account_id": account_id,
        "account_name": row.get("Name") or f"Microsoft Ads account {row.get('Number') or account_id}",
        "account_number": row.get("Number"),
        "customer_id": str(row["ParentCustomerId"]) if row.get("ParentCustomerId") else None,
        "currency": row.get("CurrencyCode") or "USD",
        "time_zone": row.get("TimeZone"),
        "is_manager": False,
    }
    status = (row.get("AccountLifeCycleStatus") or "Active").lower()
    if status not in ("active", "pause"):
        account["error"] = f"The account is {status}."
    return account


async def discover_accounts(access_token: str) -> list[dict]:
    """The ad accounts the signed-in user can open (User/Query for the user, then Accounts/Search by that user)."""
    user = await _post(f"{CUSTOMER_API}/User/Query", access_token, {"UserId": None})
    user_id = (user.get("User") or {}).get("Id")
    if not user_id:
        raise MicrosoftAdsApiError("Microsoft Ads didn't say who signed in.")
    accounts: list[dict] = []
    page = 0
    while True:
        body = await _post(f"{CUSTOMER_API}/Accounts/Search", access_token, {
            "Predicates": [{"Field": "UserId", "Operator": "Equals", "Value": str(user_id)}],
            "Ordering": [{"Field": "Name", "Order": "Ascending"}],
            "PageInfo": {"Index": page, "Size": 1000},
        })
        rows = body.get("Accounts") or []
        accounts.extend(account_from_api(r) for r in rows if r.get("Id"))
        if len(rows) < 1000:
            return accounts
        page += 1


# --- Campaigns ---

def _day(value) -> date | None:
    """A Microsoft date ({"Day", "Month", "Year"}, or a string like 2026-03-01 or 3/1/2026) -> date."""
    if not value:
        return None
    if isinstance(value, dict):
        try:
            return date(int(value["Year"]), int(value["Month"]), int(value["Day"]))
        except (KeyError, TypeError, ValueError):
            return None
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _money(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def parse_campaign(row: dict) -> dict:
    """A campaign from Campaigns/QueryByAccountId, in the shape the shared sync reads."""
    campaign_id = str(row.get("Id"))
    raw_type = str(row.get("CampaignType") or "Search").split()[0].split(",")[0]
    return {
        "campaign_id": campaign_id,
        "name": (row.get("Name") or f"Campaign {campaign_id}").strip(),
        "status": _STATUSES.get(str(row.get("Status") or "Active").lower(), "ENABLED"),
        "channel_type": _TYPES.get(raw_type.lower(), raw_type.upper() or "UNKNOWN"),
        "start_date": _day(row.get("StartDate")),
        "end_date": _day(row.get("EndDate")),
        # Microsoft budgets are daily (a shared budget's amount isn't on the campaign: then there's none)
        "daily_budget": _money(row.get("DailyBudget")),
        "total_budget": None,
        "weeks": {},
    }


async def fetch_campaign_list(access_token: str, account: dict) -> list[dict]:
    body = await _post(f"{CAMPAIGN_API}/Campaigns/QueryByAccountId", access_token,
                       {"AccountId": str(account["account_id"]), "CampaignType": CAMPAIGN_TYPES},
                       account.get("customer_id"), account["account_id"])
    return [parse_campaign(row) for row in body.get("Campaigns") or [] if row.get("Id")]


# --- Weekly performance report ---

def _date_parts(day: date) -> dict:
    return {"Day": day.day, "Month": day.month, "Year": day.year}


def report_request(account_id: str, date_from: date, date_to: date) -> dict:
    """A campaign performance report per week starting Monday, as CSV without header or footer rows."""
    return {"ReportRequest": {
        "Type": "CampaignPerformanceReportRequest",
        "ReportName": "ZenaTech Marketing Portal weekly campaigns",
        "Format": "Csv",
        "FormatVersion": "2.0",
        "ExcludeColumnHeaders": False,
        "ExcludeReportHeader": True,
        "ExcludeReportFooter": True,
        "ReturnOnlyCompleteData": False,
        "Aggregation": "WeeklyStartingMonday",
        "Columns": ["TimePeriod", "CampaignId", "Spend", "Impressions", "Clicks", "Conversions"],
        "Scope": {"AccountIds": [str(account_id)]},
        "Time": {"CustomDateRangeStart": _date_parts(date_from), "CustomDateRangeEnd": _date_parts(date_to)},
    }}


def parse_report_csv(text: str) -> list[tuple[str, date, dict]]:
    """(campaign id, week start (Monday), figures) for each row of the report CSV."""
    rows = []
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        campaign_id, week = (row.get("CampaignId") or "").strip(), _day(row.get("TimePeriod"))
        if not campaign_id or week is None:
            continue
        rows.append((campaign_id, week, {
            "spend": _money(row.get("Spend")) or Decimal("0.00"),
            "impressions": int(_money(row.get("Impressions")) or 0),
            "clicks": int(_money(row.get("Clicks")) or 0),
            "conversions": _money(row.get("Conversions")) or Decimal("0.00"),
        }))
    return rows


async def _download_report(url: str) -> str:
    """The CSV inside the zipped report (the download URL needs no sign-in)."""
    try:
        async with _http_client() as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise MicrosoftAdsApiError("Couldn't download the Microsoft Ads report. Try again in a few minutes.") from exc
    if response.status_code >= 400:
        raise MicrosoftAdsApiError(f"Couldn't download the Microsoft Ads report ({response.status_code}).")
    try:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
            return archive.read(name).decode("utf-8-sig")
    except (zipfile.BadZipFile, StopIteration) as exc:
        raise MicrosoftAdsApiError("Microsoft Ads sent a report the portal couldn't read.") from exc


async def fetch_weekly_report(access_token: str, account: dict, date_from: date, date_to: date) -> list[tuple[str, date, dict]]:
    customer, account_id = account.get("customer_id"), account["account_id"]
    submitted = await _post(f"{REPORTING_API}/GenerateReport/Submit", access_token,
                            report_request(account_id, date_from, date_to), customer, account_id)
    request_id = submitted.get("ReportRequestId")
    if not request_id:
        raise MicrosoftAdsApiError("Microsoft Ads didn't start the report.")
    for attempt in range(REPORT_POLL_ATTEMPTS):
        polled = await _post(f"{REPORTING_API}/GenerateReport/Poll", access_token,
                             {"ReportRequestId": request_id}, customer, account_id)
        status = polled.get("ReportRequestStatus") or {}
        if status.get("Status") == "Success":
            # No download link: nothing happened in the window
            return parse_report_csv(await _download_report(status["ReportDownloadUrl"])) if status.get("ReportDownloadUrl") else []
        if status.get("Status") == "Error":
            raise MicrosoftAdsApiError("Microsoft Ads couldn't make the performance report.")
        await asyncio.sleep(REPORT_POLL_SECONDS)
    raise MicrosoftAdsApiError("The Microsoft Ads report took too long. Try again later.")


async def fetch_campaigns(access_token: str, account: dict, date_from: date, date_to: date) -> list[dict]:
    """The account's campaigns with their weekly metrics from date_from to date_to."""
    campaigns = {c["campaign_id"]: c for c in await fetch_campaign_list(access_token, account)}
    for campaign_id, week, figures in await fetch_weekly_report(access_token, account, date_from, date_to):
        if campaign_id not in campaigns:
            continue
        weeks = campaigns[campaign_id]["weeks"]
        if week in weeks:
            for key, value in figures.items():
                weeks[week][key] += value
        else:
            weeks[week] = figures
    return list(campaigns.values())

