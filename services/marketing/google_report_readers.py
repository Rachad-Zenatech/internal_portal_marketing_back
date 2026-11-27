"""Google API requests and report normalization; never returns tokens or raw errors."""
import asyncio
from datetime import date
from urllib.parse import quote

import httpx

from models.marketing.google_reporting_model import GoogleReportSection
from services.marketing.google_reporting_accounts import ReaderAccount
from services.shared.core.google_reader_auth_service import get_reader_access_token


async def _post(client: httpx.AsyncClient, url: str, source: str, body: dict, manager: str | None = None):
    token = await get_reader_access_token(source)
    headers = {"Authorization": f"Bearer {token}"}
    if manager:
        headers["login-customer-id"] = manager
    response = await client.post(url, headers=headers, json=body)
    response.raise_for_status()
    return response.json()


async def read_ga4(client: httpx.AsyncClient, account: ReaderAccount, start: date, end: date) -> GoogleReportSection:
    data = await _post(client, f"https://analyticsdata.googleapis.com/v1beta/properties/{account.property_id}:runReport", "ga4", {
        "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "sessions"}, {"name": "keyEvents"}],
        "orderBys": [{"dimension": {"dimensionName": "date"}}],
        "limit": 366,
        "dimensionFilter": {"notExpression": {"filter": {
            "fieldName": "country", "inListFilter": {"values": ["Pakistan", "Philippines"]},
        }}},
    })
    rows = []
    for row in data.get("rows", []):
        day = row["dimensionValues"][0]["value"]
        metrics = row["metricValues"]
        rows.append({"date": date(int(day[:4]), int(day[4:6]), int(day[6:8])).isoformat(),
                     "sessions": int(metrics[0]["value"]), "key_events": float(metrics[1]["value"])})
    metadata = data.get("metadata", {})
    message = "Key events are configured GA4 actions, not necessarily leads."
    if metadata.get("subjectToThresholding") or metadata.get("samplingMetadatas"):
        message += " Google reports thresholding or sampling; figures may be limited."
    return GoogleReportSection(
        status="ok" if rows else "empty", rows=rows, message=message,
        totals={"sessions": sum(r["sessions"] for r in rows), "key_events": sum(r["key_events"] for r in rows)},
        time_zone=metadata.get("timeZone"),
    )


async def read_search_console(client: httpx.AsyncClient, account: ReaderAccount, start: date, end: date) -> GoogleReportSection:
    url = f"https://www.googleapis.com/webmasters/v3/sites/{quote(account.site, safe='')}/searchAnalytics/query"
    body = {
        "startDate": start.isoformat(), "endDate": end.isoformat(), "type": "web", "dataState": "final",
        "dimensionFilterGroups": [{"filters": [
            {"dimension": "country", "operator": "notEquals", "expression": country}
            for country in ("pak", "phl")
        ]}],
    }
    summary, queries = await asyncio.gather(
        _post(client, url, "gsc", body),
        _post(client, url, "gsc", {**body, "dimensions": ["query"], "rowLimit": 20}),
    )
    totals = summary.get("rows", [{}])[0] if summary.get("rows") else {}
    rows = [{"query": r["keys"][0], "clicks": r["clicks"], "impressions": r["impressions"],
             "ctr": r["ctr"], "position": r["position"]} for r in queries.get("rows", [])]
    return GoogleReportSection(
        status="ok" if totals or rows else "empty", rows=rows,
        totals={k: totals[k] for k in ("clicks", "impressions", "ctr", "position") if k in totals},
        time_zone="America/Los_Angeles",
        message="Finalized web-search data only; recent days may be absent. Up to 20 queries are shown; query rows do not represent all search traffic.",
    )


async def read_ads(client: httpx.AsyncClient, account: ReaderAccount, start: date, end: date) -> GoogleReportSection:
    if not account.ads_id:
        return GoogleReportSection(status="unavailable", message="No Google Ads account is configured for this brand.")
    query = (
        "SELECT segments.date, customer.currency_code, customer.time_zone, metrics.cost_micros, "
        "metrics.clicks, metrics.impressions, metrics.conversions FROM customer "
        f"WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}' ORDER BY segments.date"
    )
    data = await _post(client, f"https://googleads.googleapis.com/v25/customers/{account.ads_id}/googleAds:searchStream",
                       "ads", {"query": query}, account.manager_id)
    rows, currency, time_zone = [], None, None
    for batch in data:
        for result in batch.get("results", []):
            metrics = result.get("metrics", {})
            currency = result.get("customer", {}).get("currencyCode", currency)
            time_zone = result.get("customer", {}).get("timeZone", time_zone)
            rows.append({"date": result["segments"]["date"], "spend": int(metrics.get("costMicros", 0)) / 1_000_000,
                         "clicks": int(metrics.get("clicks", 0)), "impressions": int(metrics.get("impressions", 0)),
                         "conversions": float(metrics.get("conversions", 0))})
    return GoogleReportSection(
        status="ok" if rows else "empty", rows=rows, currency=currency, time_zone=time_zone,
        totals={k: sum(r[k] for r in rows) for k in ("spend", "clicks", "impressions", "conversions")} if rows else {},
        message="Spend as reported by Google Ads, without country exclusions. This is performance cost, not a billing invoice.",
    )
