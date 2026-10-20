"""Google Ads: the mode (mock or live), the app credentials, and the few REST calls the portal makes.

Everything here reads only. The portal never changes anything in Google Ads.

Mode (GOOGLE_ADS_MODE):
- "mock": no call to Google at all. Connecting stores fake tokens and the two mock accounts, and the sync generates
  campaigns and weekly results for them (services/google_ads_mock_data.py). The default outside production.
- "live": real sign-in with Google and the Google Ads REST API. The default when APP_ENV=production. Needs
  GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET (an OAuth client of the Google Cloud project that has Google Ads
  API access); without them connecting fails with NOT_SET_UP_MESSAGE.

Each connection remembers the mode it was made in (oauth_connections.connection_metadata "mode"), so mock connections
are never used as real ones and the other way round.

Calls (REST, API_VERSION):
- customers:listAccessibleCustomers: the accounts the signed-in user can open directly.
- googleAds:searchStream with GAQL: each account's name, currency, time zone and manager flag, a manager's client
  accounts, campaigns, and weekly metrics.
Headers: the access token; developer-token when GOOGLE_ADS_DEVELOPER_TOKEN is set (optional since Google moved API
access levels to the Cloud project); login-customer-id when the account is reached through a manager account
(GOOGLE_ADS_LOGIN_CUSTOMER_ID, or the manager it was found under).
"""
import logging
import os
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from services import token_crypto_service as crypto

logger = logging.getLogger(__name__)

# The one place the API version is pinned. v25 was released in July 2026 and is supported until about August 2027:
# check https://developers.google.com/google-ads/api/docs/sunset-dates before then.
API_VERSION = "v25"
API_BASE = f"https://googleads.googleapis.com/{API_VERSION}"
HTTP_TIMEOUT_SECONDS = 60.0

MOCK_MODE = "mock"
LIVE_MODE = "live"
NOT_SET_UP_MESSAGE = "Google Ads isn't set up: add GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET"
# Fake access tokens start with this (mock mode, and connections made before the mode was recorded)
MOCK_TOKEN_PREFIX = "ya29.demo_"
# Google's "no end date"
_NO_END_DATE = date(2037, 12, 30)


class GoogleAdsApiError(Exception):
    """Google Ads refused a call or couldn't be reached. The message is safe to show to users."""


def google_ads_mode() -> str:
    """GOOGLE_ADS_MODE when it's mock or live; otherwise live in production and mock everywhere else."""
    value = os.getenv("GOOGLE_ADS_MODE", "").strip().lower()
    if value in (MOCK_MODE, LIVE_MODE):
        return value
    return LIVE_MODE if os.getenv("APP_ENV", "development").strip().lower() == "production" else MOCK_MODE


def google_ads_credentials() -> tuple[str, str]:
    """The OAuth client id and secret: GOOGLE_ADS_CLIENT_ID / _SECRET, else GOOGLE_CLIENT_ID / _SECRET."""
    client_id = os.getenv("GOOGLE_ADS_CLIENT_ID") or os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_ADS_CLIENT_SECRET") or os.getenv("GOOGLE_CLIENT_SECRET", "")
    return client_id.strip(), client_secret.strip()


def google_ads_configured() -> bool:
    """Whether connecting can work in the current mode (mock mode needs nothing)."""
    if google_ads_mode() == MOCK_MODE:
        return True
    client_id, client_secret = google_ads_credentials()
    return bool(client_id and client_secret)


def connection_mode(metadata: dict | None, access_token_encrypted: str | None) -> str:
    """The mode a Google Ads connection was made in: as recorded, else mock when its token is a fake one."""
    recorded = (metadata or {}).get("mode")
    if recorded in (MOCK_MODE, LIVE_MODE):
        return recorded
    try:
        token = crypto.decrypt_token(access_token_encrypted) or ""
    except Exception:
        token = ""
    return MOCK_MODE if token.startswith(MOCK_TOKEN_PREFIX) else LIVE_MODE


def customer_digits(customer_id: str | int | None) -> str:
    """'123-456-7890' or 'customers/1234567890' -> '1234567890'."""
    return "".join(ch for ch in str(customer_id or "") if ch.isdigit())


def format_customer_id(customer_id: str | int | None) -> str:
    """'1234567890' -> '123-456-7890', as Google Ads shows it."""
    digits = customer_digits(customer_id)
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}" if len(digits) == 10 else digits


def _http_client() -> httpx.AsyncClient:
    """Replaced in tests with a client on an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)


def _headers(access_token: str, login_customer_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    developer_token = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "").strip()
    if developer_token:
        headers["developer-token"] = developer_token
    login = customer_digits(login_customer_id or os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", ""))
    if login:
        headers["login-customer-id"] = login
    return headers


def _error_message(response: httpx.Response) -> str:
    """A short, plain reason from a Google Ads error response."""
    reason = ""
    try:
        body = response.json()
        if isinstance(body, list) and body:
            body = body[0]
        error = body.get("error", {}) if isinstance(body, dict) else {}
        reason = error.get("message") or ""
        for detail in error.get("details", []) or []:
            for item in detail.get("errors", []) or []:
                if item.get("message"):
                    reason = item["message"]
                    break
    except ValueError:
        reason = ""
    if response.status_code == 401:
        return "Google Ads didn't accept the sign-in. Connect Google Ads again on OAuth Integrations."
    if response.status_code == 403:
        return f"Google Ads refused access{': ' + reason if reason else ''}."
    if response.status_code == 429:
        return "Google Ads is limiting requests right now. Try again later."
    return f"Google Ads returned an error ({response.status_code}){': ' + reason if reason else ''}."


async def _request(method: str, url: str, access_token: str, login_customer_id: str | None = None,
                   json_body: dict | None = None) -> Any:
    try:
        async with _http_client() as client:
            response = await client.request(method, url, headers=_headers(access_token, login_customer_id), json=json_body)
    except httpx.HTTPError as exc:
        logger.warning(f"Google Ads request failed: {type(exc).__name__}", extra={"event": "google_ads_request_failed"})
        raise GoogleAdsApiError("Couldn't reach Google Ads. Try again in a few minutes.") from exc
    if response.status_code >= 400:
        raise GoogleAdsApiError(_error_message(response))
    try:
        return response.json()
    except ValueError as exc:
        raise GoogleAdsApiError("Google Ads sent a reply the portal couldn't read.") from exc


async def search_stream(access_token: str, customer_id: str, query: str,
                        login_customer_id: str | None = None) -> list[dict]:
    """Every row of a GAQL query (googleAds:searchStream returns the rows in batches)."""
    url = f"{API_BASE}/customers/{customer_digits(customer_id)}/googleAds:searchStream"
    batches = await _request("POST", url, access_token, login_customer_id, {"query": query})
    if isinstance(batches, dict):
        batches = [batches]
    rows: list[dict] = []
    for batch in batches or []:
        rows.extend(batch.get("results", []) or [])
    return rows


async def list_accessible_customers(access_token: str) -> list[str]:
    """The ids (digits) of the accounts the signed-in user can open directly."""
    body = await _request("GET", f"{API_BASE}/customers:listAccessibleCustomers", access_token)
    return [customer_digits(name) for name in body.get("resourceNames", []) or [] if customer_digits(name)]


_CUSTOMER_QUERY = (
    "SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, customer.manager "
    "FROM customer LIMIT 1"
)
_CLIENTS_QUERY = (
    "SELECT customer_client.id, customer_client.descriptive_name, customer_client.currency_code, "
    "customer_client.time_zone, customer_client.manager, customer_client.status "
    "FROM customer_client WHERE customer_client.level = 1"
)


def account_from_customer(row: dict, login_customer_id: str | None = None) -> dict:
    """One account as stored in connection_metadata.discovered_accounts, from a customer or customer_client row."""
    c = row.get("customer") or row.get("customerClient") or {}
    digits = customer_digits(c.get("id"))
    account = {
        "account_id": format_customer_id(digits),
        "account_name": c.get("descriptiveName") or f"Google Ads account {format_customer_id(digits)}",
        "currency": c.get("currencyCode") or "USD",
        "time_zone": c.get("timeZone"),
        "is_manager": bool(c.get("manager")),
    }
    if login_customer_id:
        account["login_customer_id"] = format_customer_id(login_customer_id)
    return account


async def discover_accounts(access_token: str) -> list[dict]:
    """The accounts the signed-in user can see: each directly accessible one, and the client accounts directly under
    a manager account (reached through that manager). An account Google won't describe is listed with an error."""
    accounts: dict[str, dict] = {}
    for digits in await list_accessible_customers(access_token):
        try:
            rows = await search_stream(access_token, digits, _CUSTOMER_QUERY)
        except GoogleAdsApiError as exc:
            accounts[digits] = {
                "account_id": format_customer_id(digits), "account_name": f"Google Ads account {format_customer_id(digits)}",
                "currency": None, "time_zone": None, "is_manager": False, "error": str(exc),
            }
            continue
        if rows:
            accounts[digits] = account_from_customer(rows[0])
    for digits, account in list(accounts.items()):
        if not account.get("is_manager"):
            continue
        try:
            rows = await search_stream(access_token, digits, _CLIENTS_QUERY, login_customer_id=digits)
        except GoogleAdsApiError as exc:
            account["error"] = str(exc)
            continue
        for row in rows:
            client = row.get("customerClient") or {}
            child = customer_digits(client.get("id"))
            if child and child not in accounts and client.get("status", "ENABLED") == "ENABLED":
                accounts[child] = account_from_customer(row, login_customer_id=digits)
    return list(accounts.values())


# --- Campaigns and weekly metrics ---

def campaigns_query() -> str:
    return (
        "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
        "campaign.start_date_time, campaign.end_date_time, campaign_budget.amount_micros, "
        "campaign_budget.total_amount_micros, campaign_budget.period "
        "FROM campaign"
    )


def metrics_query(date_from: date, date_to: date) -> str:
    return (
        "SELECT campaign.id, segments.week, metrics.cost_micros, metrics.impressions, metrics.clicks, "
        "metrics.conversions FROM campaign "
        f"WHERE segments.date BETWEEN '{date_from.isoformat()}' AND '{date_to.isoformat()}'"
    )


def _micros(value) -> Decimal | None:
    if value in (None, ""):
        return None
    return (Decimal(str(value)) / Decimal(1_000_000)).quantize(Decimal("0.01"))


def _day(value) -> date | None:
    """'2026-03-01' or '2026-03-01 00:00:00' -> date; Google's 'no end date' -> None."""
    if not value:
        return None
    try:
        day = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return None if day >= _NO_END_DATE else day


def parse_campaign_row(row: dict) -> dict:
    """A campaign from a GAQL row (REST JSON uses camelCase)."""
    campaign = row.get("campaign") or {}
    budget = row.get("campaignBudget") or {}
    daily = _micros(budget.get("amountMicros")) if budget.get("period", "DAILY") == "DAILY" else None
    return {
        "campaign_id": str(campaign.get("id")),
        "name": (campaign.get("name") or f"Campaign {campaign.get('id')}").strip(),
        "status": campaign.get("status") or "ENABLED",
        "channel_type": campaign.get("advertisingChannelType") or "UNKNOWN",
        "start_date": _day(campaign.get("startDateTime") or campaign.get("startDate")),
        "end_date": _day(campaign.get("endDateTime") or campaign.get("endDate")),
        "daily_budget": daily,
        "total_budget": _micros(budget.get("totalAmountMicros")),
        "weeks": {},
    }


def parse_metric_row(row: dict) -> tuple[str, date, dict] | None:
    """(campaign id, week start (Monday), figures) from a weekly metrics row."""
    campaign = row.get("campaign") or {}
    week = _day((row.get("segments") or {}).get("week"))
    if not campaign.get("id") or week is None:
        return None
    metrics = row.get("metrics") or {}
    return str(campaign["id"]), week, {
        "spend": _micros(metrics.get("costMicros")) or Decimal("0.00"),
        "impressions": int(metrics.get("impressions") or 0),
        "clicks": int(metrics.get("clicks") or 0),
        "conversions": Decimal(str(metrics.get("conversions") or 0)).quantize(Decimal("0.01")),
    }


async def fetch_campaigns(access_token: str, account: dict, date_from: date, date_to: date) -> list[dict]:
    """The account's campaigns with their weekly metrics from date_from to date_to (two queries)."""
    login = account.get("login_customer_id")
    customer = account["account_id"]
    campaigns = {}
    for row in await search_stream(access_token, customer, campaigns_query(), login):
        parsed = parse_campaign_row(row)
        campaigns[parsed["campaign_id"]] = parsed
    for row in await search_stream(access_token, customer, metrics_query(date_from, date_to), login):
        parsed = parse_metric_row(row)
        if parsed is None or parsed[0] not in campaigns:
            continue
        campaign_id, week, figures = parsed
        weeks = campaigns[campaign_id]["weeks"]
        if week in weeks:
            for key, value in figures.items():
                weeks[week][key] += value
        else:
            weeks[week] = figures
    return list(campaigns.values())
