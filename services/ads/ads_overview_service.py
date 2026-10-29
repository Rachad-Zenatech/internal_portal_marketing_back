"""The Paid Advertising page: key figures, charts, campaigns needing attention, one campaign's detail, data health.

Every figure comes from ad results (services/shared/ads_service.py), one currency at a time; money is never added across
currencies. Platform conversions and verified leads (HubSpot contacts) are kept apart and never added together.

How the dates work:
- A result counts in a date range when its period starts in the range (the same day its spend line is dated), so
  a result is never counted in two adjacent ranges. The previous period is the same number of days right before.
- Allocated budget: the campaign's approved budget spread evenly over its dates, the share that falls in the range.
  A campaign without a budget or without both dates uses the budgets entered on its results instead.
- A campaign shows for a range when it has results in it or its dates overlap it, and for a range that reaches
  today, also when it starts later (upcoming). Drafts and cancelled campaigns only show when they have results in it,
  or when the status filter asks for them.

Campaigns needing attention (worked out for today, whatever the range):
- Budget nearly used up: 90% or more of the campaign's budget spent so far.
- Cost per lead too high: in the range, above the campaign's KPI target when its primary KPI is a cost per lead (or
  per MQL); otherwise 30% or more above its platform's cost per lead in the range.
- Paused while its dates say it should be running.
- Data delayed: an active, running campaign without results in the last 7 days, or whose platform's API connection
  isn't connected.
"""
import json
from collections import defaultdict
from datetime import date, timedelta
from uuid import UUID

from postgresql_db.database import get_pool
from services.ads import google_ads_client, microsoft_ads_client
from services.shared.ads_service import CAMPAIGN_JOINS, PLATFORMS

# --- Rules ---
BUDGET_ALERT_PERCENT = 90.0
CPL_ABOVE_PLATFORM = 1.3
STALE_DAYS = 7
# Pace: spent share of the budget against the share of the campaign's days gone by, in percentage points
PACE_TOLERANCE = 10.0
# Charts: one point per day up to this many days, then per week, then per month
DAILY_UP_TO_DAYS = 14
WEEKLY_UP_TO_DAYS = 366

# The ad platforms each API connection (oauth_connections.provider) covers
CONNECTION_PROVIDERS = {
    "GOOGLE_ADS": "GOOGLE_ADS", "YOUTUBE_ADS": "GOOGLE_ADS", "BING_ADS": "MICROSOFT_ADS",
    "META_ADS": "META_ADS", "LINKEDIN_ADS": "LINKEDIN_ADS",
}
# Providers with a campaign and results sync: (the mode a connection was made in, the current mode), and the sync's
# integration_sync_state name in live mode ("_mock" added in mock mode)
SYNCED_PROVIDERS = {
    "GOOGLE_ADS": (google_ads_client.connection_mode, google_ads_client.google_ads_mode),
    "MICROSOFT_ADS": (microsoft_ads_client.connection_mode, microsoft_ads_client.microsoft_ads_mode),
}
SYNC_STATE_NAMES = {"GOOGLE_ADS": "google_ads", "MICROSOFT_ADS": "microsoft_ads"}
# Primary KPIs that are a cost per lead: the "cost per lead too high" rule compares with their target
COST_PER_LEAD_KPIS = {"COST_PER_LEAD": "cpl", "COST_PER_MQL": "cost_per_mql"}
# Objective -> the figure that says whether the campaign works
OBJECTIVE_METRICS = {
    "AWARENESS": "CPM", "VIDEO_VIEWS": "CPM", "TRAFFIC": "CPC", "ENGAGEMENT": "CPE", "LEADS": "CPL",
    "CONVERSIONS": "CPA",
}
METRIC_LABELS = {
    "CPM": "Cost per 1,000 impressions", "CPC": "Cost per click", "CPE": "Cost per engagement",
    "CPL": "Cost per lead", "CPA": "Cost per conversion",
}
# A primary KPI that is the same figure as the success metric, so its target applies
KPI_FOR_METRIC = {"CPL": "COST_PER_LEAD", "CPC": "CPC"}
# Statuses only shown when they have results in the range, or the status filter asks for them
HIDDEN_STATUSES = {"DRAFT", "CANCELLED"}

_FIGURES = ("spend", "impressions", "clicks", "conversions", "verified_leads", "mqls", "sqls")


# --- Small calculations (pure, so they're easy to test) ---

def ratio(numerator, denominator, scale: float = 1) -> float | None:
    """numerator / denominator (x scale) to 2 dp; None when either is missing or the divisor is 0."""
    if numerator is None or not denominator:
        return None
    return round(float(numerator) / float(denominator) * scale, 2)


def _money(value) -> float | None:
    return None if value is None else round(float(value), 2)


def previous_range(date_from: date, date_to: date) -> tuple[date, date]:
    """The same number of days right before."""
    days = (date_to - date_from).days + 1
    return date_from - timedelta(days=days), date_from - timedelta(days=1)


def overlap_days(start: date, end: date, date_from: date, date_to: date) -> int:
    return max(0, (min(end, date_to) - max(start, date_from)).days + 1)


def allocated_budget(campaign: dict, date_from: date, date_to: date, results_budget: float | None) -> float | None:
    """The campaign's budget spread evenly over its dates, the share in [from, to]; else its results' budgets."""
    budget, start, end = campaign.get("budget_amount"), campaign.get("start_date"), campaign.get("end_date")
    if budget and start and end and end >= start:
        return budget * overlap_days(start, end, date_from, date_to) / ((end - start).days + 1)
    return results_budget


def figures_of(rows: list[dict]) -> dict:
    """Sums of the result figures; a figure no row has stays None."""
    out = {}
    for f in _FIGURES:
        values = [r[f] for r in rows if r.get(f) is not None]
        out[f] = sum(values) if values else None
    return out


def ratios(figures: dict) -> dict:
    spend = figures.get("spend")
    return {
        "ctr": ratio(figures.get("clicks"), figures.get("impressions"), 100),
        "cpc": ratio(spend, figures.get("clicks")),
        "cpm": ratio(spend, figures.get("impressions"), 1000),
        "conversion_rate": ratio(figures.get("conversions"), figures.get("clicks"), 100),
        "cpl": ratio(spend, figures.get("verified_leads")),
        "cost_per_mql": ratio(spend, figures.get("mqls")),
        "cost_per_conversion": ratio(spend, figures.get("conversions")),
    }


def success_metric(objective: str | None, kpi_code: str | None, kpi_target: float | None, values: dict) -> dict:
    """The figure that says whether the campaign works, from its objective, and its target when it has one.

    Awareness and video views: cost per 1,000 impressions. Traffic: cost per click. Engagement: cost per engagement
    (clicks, as platforms don't report engagements here). Leads: cost per verified lead. Conversions: cost per
    conversion. No objective: the primary KPI's figure when it is one of these, otherwise cost per lead.
    """
    code = OBJECTIVE_METRICS.get(objective or "")
    if code is None:
        code = next((m for m, k in KPI_FOR_METRIC.items() if k == kpi_code), "CPL")
    value = {
        "CPM": values.get("cpm"), "CPC": values.get("cpc"), "CPE": values.get("cpc"), "CPL": values.get("cpl"),
        "CPA": values.get("cost_per_conversion"),
    }[code]
    target = kpi_target if kpi_code is not None and KPI_FOR_METRIC.get(code) == kpi_code else None
    return {
        "code": code, "label": METRIC_LABELS[code], "value": value, "target": target,
        "on_target": None if value is None or target is None else value <= target,
    }


def primary_kpi_value(code: str | None, range_ratios: dict, to_date: dict) -> float | None:
    """The primary KPI's figure: costs and rates for the date range, counts since the campaign started."""
    if code is None:
        return None
    spend = to_date.get("spend")
    simple = {
        "COST_PER_LEAD": range_ratios.get("cpl"), "COST_PER_MQL": range_ratios.get("cost_per_mql"),
        "CPC": range_ratios.get("cpc"), "CTR": range_ratios.get("ctr"),
        "CONVERSION_RATE": range_ratios.get("conversion_rate"),
        "LEADS": to_date.get("verified_leads"), "MQLS": to_date.get("mqls"), "SQLS": to_date.get("sqls"),
        "CONVERSIONS": to_date.get("conversions"), "IMPRESSIONS": to_date.get("impressions"),
        "DEMOS": to_date.get("demos"), "CUSTOMERS": to_date.get("customers"),
        "CAC": ratio(spend, to_date.get("customers")),
    }
    if code == "ROI":
        revenue = to_date.get("revenue")
        return None if revenue is None or not spend else round((revenue - spend) / spend * 100, 2)
    value = simple.get(code)
    return None if value is None else round(float(value), 2)


def kpi_on_target(unit: str | None, value: float | None, target: float | None) -> bool | None:
    """Money KPIs (costs) are on target at or below it; counts and rates at or above it."""
    if value is None or target is None or unit is None:
        return None
    return value <= target if unit == "MONEY" else value >= target


def is_running(campaign: dict, today: date) -> bool:
    """Its dates say it runs today."""
    start, end = campaign.get("start_date"), campaign.get("end_date")
    return start is not None and start <= today and (end is None or end >= today)


def data_health(campaign: dict, last_data_date: date | None, latest_source: str | None, connection: str | None,
                today: date) -> dict:
    """Where the campaign's results come from and whether they're up to date.

    connection: the best status of its platform's API connections, or None when there's none.
    Status: NOT_STARTED, OK, STALE (nothing in the last 7 days), NO_DATA (never), CONNECTION (API not connected).
    """
    source = "API" if connection == "CONNECTED" else latest_source
    started = campaign.get("start_date") is None or campaign["start_date"] <= today
    if campaign["status"] in ("DRAFT", "PLANNED") or not started:
        status = "NOT_STARTED"
    elif connection is not None and connection != "CONNECTED":
        status = "CONNECTION"
    elif last_data_date is None:
        status = "NO_DATA"
    elif campaign["status"] == "ACTIVE" and is_running(campaign, today) and last_data_date < today - timedelta(days=STALE_DAYS):
        status = "STALE"
    else:
        status = "OK"
    return {"status": status, "source": source, "last_data_date": last_data_date, "connection_status": connection}


def attention_reasons(row: dict, platform_cpl: dict[str, float | None], platform_lead_campaigns: dict[str, int],
                      today: date) -> list[dict]:
    """Why a campaign needs a look (see the module docstring), most serious first."""
    reasons = []
    used = row.get("budget_used_percent")
    if used is not None and used >= BUDGET_ALERT_PERCENT and row["status"] not in ("COMPLETED", "CANCELLED"):
        reasons.append({"kind": "BUDGET", "severity": "HIGH" if used >= 100 else "MEDIUM", "value": used,
                        "benchmark": BUDGET_ALERT_PERCENT})

    kpi = row.get("primary_kpi") or {}
    cost_kpi = COST_PER_LEAD_KPIS.get(kpi.get("code") or "")
    target = row.get("kpi_target")
    if cost_kpi and target is not None:
        value = row.get(cost_kpi)
        if value is not None and value > target:
            reasons.append({"kind": "CPL_ABOVE_TARGET", "severity": "HIGH" if value >= target * 1.5 else "MEDIUM",
                            "value": value, "benchmark": target})
    else:
        value, average = row.get("cpl"), platform_cpl.get(row["platform"])
        # Only against other campaigns: a platform with one campaign would compare it with itself
        if (value is not None and average and platform_lead_campaigns.get(row["platform"], 0) >= 2
                and value >= average * CPL_ABOVE_PLATFORM):
            reasons.append({"kind": "CPL_ABOVE_PLATFORM", "severity": "HIGH" if value >= average * 1.6 else "MEDIUM",
                            "value": value, "benchmark": average})

    if row["status"] == "PAUSED" and is_running(row, today):
        reasons.append({"kind": "UNEXPECTEDLY_PAUSED", "severity": "MEDIUM", "value": None, "benchmark": None})

    health = row["data_health"]
    started_a_while_ago = row.get("start_date") is not None and row["start_date"] <= today - timedelta(days=STALE_DAYS)
    if row["status"] == "ACTIVE" and is_running(row, today) and (
        health["status"] in ("STALE", "CONNECTION") or (health["status"] == "NO_DATA" and started_a_while_ago)
    ):
        reasons.append({"kind": "DATA_DELAYED", "severity": "MEDIUM", "value": None, "benchmark": None})
    order = {"HIGH": 0, "MEDIUM": 1}
    return sorted(reasons, key=lambda r: order[r["severity"]])


def buckets(date_from: date, date_to: date) -> tuple[str, list[tuple[date, date]]]:
    """Chart points over the range: days, Monday-to-Sunday weeks, or months, each cut to the range."""
    days = (date_to - date_from).days + 1
    out = []
    if days <= DAILY_UP_TO_DAYS:
        return "day", [(date_from + timedelta(days=n), date_from + timedelta(days=n)) for n in range(days)]
    if days <= WEEKLY_UP_TO_DAYS:
        start = date_from - timedelta(days=date_from.weekday())
        while start <= date_to:
            out.append((max(start, date_from), min(start + timedelta(days=6), date_to)))
            start += timedelta(days=7)
        return "week", out
    start = date_from.replace(day=1)
    while start <= date_to:
        following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((max(start, date_from), min(following - timedelta(days=1), date_to)))
        start = following
    return "month", out


def months(date_from: date, date_to: date) -> list[tuple[date, date]]:
    """Each calendar month the range touches, cut to the range."""
    out, start = [], date_from.replace(day=1)
    while start <= date_to:
        following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((max(start, date_from), min(following - timedelta(days=1), date_to)))
        start = following
    return out


def shows_in_range(campaign: dict, has_results: bool, date_from: date, date_to: date, today: date,
                   status_filter: str | None) -> bool:
    if has_results:
        return True
    if campaign["status"] in HIDDEN_STATUSES and status_filter != campaign["status"]:
        return False
    start, end = campaign.get("start_date"), campaign.get("end_date")
    if start is None:
        return campaign["status"] in ("ACTIVE", "PAUSED", "PLANNED") or status_filter == campaign["status"]
    if start <= date_to and (end is None or end >= date_from):
        return True
    # Looking at a range that reaches today: upcoming campaigns too
    return date_to >= today and start > today


# --- Reading ---

async def _fetch_campaigns(conn, filters: dict, ids: list[UUID] | None = None) -> list[dict]:
    clauses, args = [], []

    def where(sql: str, value) -> None:
        args.append(value)
        clauses.append(sql.format(f"${len(args)}"))

    if filters.get("brand_id") is not None:
        where("i.brand_id = {}", filters["brand_id"])
    if filters.get("market_id") is not None:
        where("i.market_id = {}", filters["market_id"])
    if filters.get("platform"):
        where("sc.code = {}", filters["platform"])
    if filters.get("objective"):
        where("i.channel_details->>'campaign_objective' = {}", filters["objective"])
    if filters.get("status"):
        where("i.status = {}", filters["status"])
    if ids is not None:
        where("i.id = ANY({}::uuid[])", ids)
    rows = await conn.fetch(
        f"""
        SELECT i.id AS initiative_id, i.name, i.description, i.brand_id, b.name AS brand_name, i.market_id,
               mk.name AS market_name, sc.code AS platform, ch.code AS channel,
               i.channel_details->>'campaign_objective' AS objective,
               i.channel_details->>'ad_account_id' AS ad_account_id, i.status, i.start_date, i.end_date,
               i.currency::text AS currency, i.budget_amount::float8 AS budget_amount,
               i.kpi_target::float8 AS kpi_target, pk.code AS kpi_code, pk.name AS kpi_name, pk.unit AS kpi_unit,
               u.full_name AS owner_name, i.data_source
        FROM initiatives i
        {CAMPAIGN_JOINS}
        JOIN brands b ON b.id = i.brand_id
        LEFT JOIN markets mk ON mk.id = i.market_id
        LEFT JOIN primary_kpis pk ON pk.id = i.primary_kpi_id
        LEFT JOIN users u ON u.id = i.owner_user_id
        {'WHERE ' + ' AND '.join(clauses) if clauses else ''}
        ORDER BY lower(i.name), i.id
        """,
        *args,
    )
    return [dict(r) for r in rows]


_RESULT_SUMS = """
    SUM(r.budget_amount)::float8 AS budget, SUM(r.spend)::float8 AS spend, SUM(r.impressions)::float8 AS impressions,
    SUM(r.clicks)::float8 AS clicks, SUM(r.conversions)::float8 AS conversions,
    SUM(r.hubspot_contacts)::float8 AS verified_leads, SUM(r.mqls)::float8 AS mqls, SUM(r.sqls)::float8 AS sqls
"""


async def _fetch_daily(conn, ids: list[UUID], date_from: date, date_to: date) -> dict[UUID, list[dict]]:
    """Each campaign's results by period start, for results starting in [from, to]."""
    rows = await conn.fetch(
        f"""
        SELECT r.initiative_id, r.period_start AS day, {_RESULT_SUMS}
        FROM ad_results r
        WHERE r.initiative_id = ANY($1::uuid[]) AND r.period_start BETWEEN $2 AND $3
        GROUP BY r.initiative_id, r.period_start
        """,
        ids, date_from, date_to,
    )
    out: dict[UUID, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["initiative_id"]].append(dict(r))
    return out


async def _fetch_to_date(conn, ids: list[UUID], today: date) -> dict[UUID, dict]:
    """Each campaign's results so far (every result), with its latest data date, source and report link,
    and the CRM results recorded for it (demos, customers, revenue in its currency)."""
    rows = await conn.fetch(
        f"""
        WITH res AS (
            SELECT r.initiative_id, {_RESULT_SUMS}, count(*) AS result_count,
                   MAX(LEAST(COALESCE(r.period_end, r.period_start), $2)) FILTER (WHERE r.period_start <= $2)
                       AS last_data_date,
                   (array_agg(r.source ORDER BY r.period_start DESC, r.updated_at DESC))[1] AS latest_source,
                   (array_agg(r.report_url ORDER BY r.period_start DESC) FILTER (WHERE r.report_url IS NOT NULL))[1]
                       AS report_url,
                   MAX(r.updated_at) AS updated_at
            FROM ad_results r WHERE r.initiative_id = ANY($1::uuid[])
            GROUP BY r.initiative_id
        ),
        -- Where a synced value and a typed-in one exist for the same month and result, the synced one counts
        picked AS (
            SELECT DISTINCT ON (o.initiative_id, o.period_start, o.metric_id) o.initiative_id, o.metric_id, o.value,
                   o.currency
            FROM outcomes o WHERE o.initiative_id = ANY($1::uuid[])
            ORDER BY o.initiative_id, o.period_start, o.metric_id, (o.source = 'MANUAL'), o.recorded_at DESC
        ),
        crm AS (
            SELECT p.initiative_id,
                   SUM(p.value) FILTER (WHERE m.code = 'DEMOS')::float8 AS demos,
                   SUM(p.value) FILTER (WHERE m.code = 'CUSTOMERS')::float8 AS customers,
                   SUM(p.value) FILTER (WHERE m.code = 'REVENUE' AND p.currency = i.currency)::float8 AS revenue
            FROM picked p
            JOIN outcome_metrics m ON m.id = p.metric_id
            JOIN initiatives i ON i.id = p.initiative_id
            GROUP BY p.initiative_id
        )
        SELECT COALESCE(res.initiative_id, crm.initiative_id) AS initiative_id, res.*, crm.demos, crm.customers,
               crm.revenue
        FROM res FULL JOIN crm ON crm.initiative_id = res.initiative_id
        """,
        ids, today,
    )
    return {r["initiative_id"]: dict(r) for r in rows}


async def _fetch_connections(conn) -> dict[str, list[dict]]:
    """API connections per provider, if the teammate's oauth_connections table is there (read only)."""
    exists = await conn.fetchval("SELECT to_regclass('public.oauth_connections') IS NOT NULL")
    if not exists:
        return {}
    rows = await conn.fetch(
        """
        SELECT provider, status, account_name, provider_account_id, updated_at, connection_metadata,
               access_token_encrypted
        FROM oauth_connections WHERE provider = ANY($1::text[])
        ORDER BY provider, (status = 'CONNECTED') DESC, updated_at DESC
        """,
        sorted(set(CONNECTION_PROVIDERS.values())),
    )
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        row = dict(r)
        meta, token = row.pop("connection_metadata"), row.pop("access_token_encrypted")
        # Google Ads and Microsoft Ads connections made in the other mode (mock or live) don't count: mock ones are
        # never real
        if r["provider"] in SYNCED_PROVIDERS:
            mode_of, sync_mode = SYNCED_PROVIDERS[r["provider"]]
            if isinstance(meta, str):
                meta = json.loads(meta or "{}")
            if mode_of(meta, token) != sync_mode():
                continue
        out[r["provider"]].append(row)
    return out


async def _fetch_ad_syncs(conn) -> dict[str, dict]:
    """Per provider with a sync (Google Ads, Microsoft Ads): when it last worked in the current mode, and its mode."""
    modes = {provider: sync_mode() for provider, (_, sync_mode) in SYNCED_PROVIDERS.items()}
    names = {f"{SYNC_STATE_NAMES[p]}_mock" if mode == "mock" else SYNC_STATE_NAMES[p]: p for p, mode in modes.items()}
    rows = await conn.fetch(
        "SELECT name, last_success_at, last_attempt_at, last_error FROM integration_sync_state WHERE name = ANY($1::text[])",
        list(names),
    )
    return {names[r["name"]]: {**dict(r), "mode": modes[names[r["name"]]]} for r in rows}


def connection_status(platform: str, connections: dict[str, list[dict]]) -> str | None:
    """CONNECTED when any connection for the platform's provider is; else the latest one's status; None if none."""
    found = connections.get(CONNECTION_PROVIDERS.get(platform, ""), [])
    if not found:
        return None
    return "CONNECTED" if any(c["status"] == "CONNECTED" for c in found) else found[0]["status"]


def _sum_days(days: list[dict], date_from: date, date_to: date) -> list[dict]:
    return [d for d in days if date_from <= d["day"] <= date_to]


def _kpis(rows: list[dict]) -> dict:
    """Key figures over campaign rows (one currency). Verified leads and conversions are never added together."""
    figures = figures_of(rows)
    allocated_values = [r["allocated_budget"] for r in rows if r["allocated_budget"] is not None]
    allocated = sum(allocated_values) if allocated_values else None
    remaining = None if allocated is None and figures["spend"] is None else (allocated or 0) - (figures["spend"] or 0)
    calc = ratios(figures)
    return {
        "allocated_budget": _money(allocated),
        "actual_spend": _money(figures["spend"]),
        "budget_remaining": _money(remaining),
        "remaining_percent": ratio(remaining, allocated, 100),
        "verified_leads": figures["verified_leads"],
        "cost_per_lead": calc["cpl"],
        "conversions": _money(figures["conversions"]),
        "impressions": figures["impressions"],
        "clicks": figures["clicks"],
        "ctr": calc["ctr"],
        "cpc": calc["cpc"],
        "mqls": figures["mqls"],
        "sqls": figures["sqls"],
        "campaign_count": len(rows),
        "active_campaign_count": sum(1 for r in rows if r["status"] == "ACTIVE"),
    }


def _range_row(campaign: dict, days: list[dict], date_from: date, date_to: date) -> dict:
    """The campaign's figures for one range."""
    in_range = _sum_days(days, date_from, date_to)
    results_budget = [d["budget"] for d in in_range if d["budget"] is not None]
    figures = figures_of(in_range)
    return {
        **figures,
        "allocated_budget": allocated_budget(campaign, date_from, date_to, sum(results_budget) if results_budget else None),
        "has_results": bool(in_range),
    }


def matches(campaign: dict, filters: dict) -> bool:
    """The page filters (brand, market, platform, objective, status); currency is handled separately."""
    for key in ("brand_id", "market_id", "platform", "objective", "status"):
        wanted = filters.get(key)
        if wanted is not None and wanted != "" and campaign.get(key) != wanted:
            return False
    return True


async def _compute(filters: dict, date_from: date, date_to: date, today: date, include_ids: list[UUID] | None = None) -> dict:
    """Everything the page needs for the filters and range, in one currency (filters["currency"] or the most used).

    Every campaign is read, so each one is compared with its whole platform whatever the filters (the platform's
    cost per lead in the range and currency); the filters then pick the campaigns shown and summed.
    """
    prev_from, prev_to = previous_range(date_from, date_to)
    pool = get_pool()
    async with pool.acquire() as conn:
        campaigns = await _fetch_campaigns(conn, {})
        ids = [c["initiative_id"] for c in campaigns]
        daily = await _fetch_daily(conn, ids, min(prev_from, date_from), date_to) if ids else {}
        to_date = await _fetch_to_date(conn, ids, today) if ids else {}
        connections = await _fetch_connections(conn)

    # One currency at a time: the one asked for, else the one most matching campaigns use (USD first on a tie)
    counts: dict[str, int] = defaultdict(int)
    for c in campaigns:
        if matches(c, filters):
            counts[c["currency"]] += 1
    currencies = sorted(counts, key=lambda cur: (-counts[cur], cur != "USD", cur))
    currency = filters.get("currency") or (currencies[0] if currencies else "USD")

    status_filter = filters.get("status")
    current, previous, benchmark = [], [], []
    for c in campaigns:
        if c["currency"] != currency:
            continue
        days = daily.get(c["initiative_id"], [])
        now = {**c, **_range_row(c, days, date_from, date_to), "days": days}
        before = {**c, **_range_row(c, days, prev_from, prev_to)}
        forced = include_ids is not None and c["initiative_id"] in include_ids
        shown_now = shows_in_range(c, now["has_results"], date_from, date_to, today, status_filter)
        if shown_now:
            benchmark.append(now)
        if not matches(c, filters) and not forced:
            continue
        if shown_now or forced:
            current.append(now)
        if shows_in_range(c, before["has_results"], prev_from, prev_to, today, status_filter):
            previous.append(before)

    # Each platform's cost per lead over all its campaigns, to compare each campaign with
    platform_cpl, platform_lead_campaigns = {}, {}
    for platform in PLATFORMS:
        rows = [r for r in benchmark if r["platform"] == platform]
        platform_cpl[platform] = ratios(figures_of(rows))["cpl"]
        platform_lead_campaigns[platform] = sum(1 for r in rows if r["verified_leads"])

    # Per platform, with the previous period for the change
    by_platform = []
    for platform in PLATFORMS:
        rows = [r for r in current if r["platform"] == platform]
        before = [r for r in previous if r["platform"] == platform]
        if not rows and not before:
            continue
        k, p = _kpis(rows), _kpis(before)
        by_platform.append({
            "platform": platform, "campaign_count": k["campaign_count"], "allocated_budget": k["allocated_budget"],
            "spend": k["actual_spend"], "impressions": k["impressions"], "clicks": k["clicks"], "ctr": k["ctr"],
            "cpc": k["cpc"], "conversions": k["conversions"], "verified_leads": k["verified_leads"],
            "cpl": k["cost_per_lead"], "mqls": k["mqls"], "sqls": k["sqls"],
            "budget_used_percent": ratio(k["actual_spend"], k["allocated_budget"], 100),
            "previous_spend": p["actual_spend"], "previous_verified_leads": p["verified_leads"],
            "previous_cpl": p["cost_per_lead"],
        })

    rows = []
    for r in current:
        so_far = to_date.get(r["initiative_id"], {})
        to_date_figures = {
            "spend": so_far.get("spend"), "impressions": so_far.get("impressions"), "clicks": so_far.get("clicks"),
            "conversions": so_far.get("conversions"), "verified_leads": so_far.get("verified_leads"),
            "mqls": so_far.get("mqls"), "sqls": so_far.get("sqls"), "demos": so_far.get("demos"),
            "customers": so_far.get("customers"), "revenue": so_far.get("revenue"),
        }
        budget = r["budget_amount"] if r["budget_amount"] else so_far.get("budget")
        calc = ratios(r)
        kpi = {"code": r["kpi_code"], "name": r["kpi_name"], "unit": r["kpi_unit"]} if r["kpi_code"] else None
        kpi_value = primary_kpi_value(r["kpi_code"], calc, to_date_figures)
        row = {
            key: r[key] for key in (
                "initiative_id", "name", "description", "brand_id", "brand_name", "market_id", "market_name",
                "platform", "channel", "objective", "ad_account_id", "status", "start_date", "end_date", "currency",
                "kpi_target", "owner_name", "data_source",
            )
        }
        row.update(
            budget_amount=_money(budget),
            spend_to_date=_money(so_far.get("spend")),
            budget_used_percent=ratio(so_far.get("spend"), budget, 100),
            allocated_budget=_money(r["allocated_budget"]),
            spend=_money(r["spend"]), impressions=r["impressions"], clicks=r["clicks"],
            conversions=_money(r["conversions"]), verified_leads=r["verified_leads"], mqls=r["mqls"], sqls=r["sqls"],
            **calc,
            revenue_to_date=_money(so_far.get("revenue")),
            roas=ratio(so_far.get("revenue"), so_far.get("spend")),
            primary_kpi=kpi,
            primary_kpi_value=kpi_value,
            primary_kpi_on_target=kpi_on_target(r["kpi_unit"], kpi_value, r["kpi_target"]),
            success_metric=success_metric(r["objective"], r["kpi_code"], r["kpi_target"], calc),
            report_url=so_far.get("report_url"),
            result_count=so_far.get("result_count") or 0,
            data_health=data_health(
                r, so_far.get("last_data_date"), so_far.get("latest_source"),
                connection_status(r["platform"], connections), today,
            ),
        )
        row["attention"] = attention_reasons(row, platform_cpl, platform_lead_campaigns, today)
        row["_days"] = r["days"]
        row["_to_date"] = to_date_figures
        rows.append(row)

    return {
        "currency": currency, "currencies": currencies, "rows": rows, "current": current, "previous": previous,
        "by_platform": by_platform, "prev_from": prev_from, "prev_to": prev_to, "connections": connections,
    }


def _series(rows: list[dict], date_from: date, date_to: date) -> tuple[str, list[dict]]:
    kind, ranges = buckets(date_from, date_to)
    points = []
    for start, end in ranges:
        days = [d for r in rows for d in _sum_days(r["_days"], start, end)]
        f = figures_of(days)
        points.append({
            "start": start, "end": end, "spend": _money(f["spend"]) or 0, "verified_leads": f["verified_leads"] or 0,
            "conversions": _money(f["conversions"]) or 0, "impressions": f["impressions"] or 0,
            "clicks": f["clicks"] or 0,
        })
    return kind, points


async def overview(filters: dict, date_from: date, date_to: date, today: date | None = None) -> dict:
    """Key figures with the previous period, per platform, brand and month, the chart over time, the campaigns
    needing attention and every campaign row, for one currency."""
    today = today or date.today()
    data = await _compute(filters, date_from, date_to, today)
    rows = data["rows"]
    kpis = _kpis(rows)
    previous = _kpis(data["previous"])

    by_brand = defaultdict(list)
    for r in rows:
        by_brand[(r["brand_id"], r["brand_name"])].append(r)
    brands = []
    for (brand_id, brand_name), items in by_brand.items():
        k = _kpis(items)
        brands.append({
            "brand_id": brand_id, "brand_name": brand_name, "campaign_count": k["campaign_count"],
            "allocated_budget": k["allocated_budget"], "spend": k["actual_spend"],
            "budget_used_percent": ratio(k["actual_spend"], k["allocated_budget"], 100),
            "verified_leads": k["verified_leads"], "cpl": k["cost_per_lead"],
        })
    brands.sort(key=lambda b: (-(b["allocated_budget"] or 0), b["brand_name"].lower()))

    month_rows = []
    for start, end in months(date_from, date_to):
        allocated = [
            allocated_budget(r, start, end, sum(d["budget"] for d in _sum_days(r["days"], start, end) if d["budget"] is not None) or None)
            for r in data["current"]
        ]
        allocated = [a for a in allocated if a is not None]
        f = figures_of([d for r in rows for d in _sum_days(r["_days"], start, end)])
        month_rows.append({
            "month": start.replace(day=1), "start": start, "end": end,
            "allocated_budget": _money(sum(allocated)) if allocated else None, "spend": _money(f["spend"]),
            "verified_leads": f["verified_leads"],
        })

    bucket, series = _series(rows, date_from, date_to)
    attention = [
        {
            "initiative_id": r["initiative_id"], "name": r["name"], "platform": r["platform"],
            "brand_name": r["brand_name"], **reason,
        }
        for r in rows for reason in r["attention"]
    ]
    attention.sort(key=lambda a: ({"HIGH": 0, "MEDIUM": 1}[a["severity"]], a["name"].lower()))
    for r in rows:
        r.pop("_days")
        r.pop("_to_date")
    return {
        "date_from": date_from, "date_to": date_to, "previous_from": data["prev_from"],
        "previous_to": data["prev_to"], "today": today, "currency": data["currency"],
        "currencies": data["currencies"], "kpis": kpis, "previous": previous, "by_platform": data["by_platform"],
        "by_brand": brands, "by_month": month_rows, "bucket": bucket, "timeseries": series,
        "attention": attention, "campaigns": rows,
    }


class CampaignNotFoundError(Exception):
    """No ad campaign with that id."""


def pace(budget: float | None, spent: float | None, start: date | None, end: date | None, today: date) -> dict:
    """Spent share of the budget against the share of the campaign's days gone by."""
    used = ratio(spent or 0, budget, 100) if budget else None
    if not (start and end and end >= start):
        return {"time_elapsed_percent": None, "budget_used_percent": used, "status": None, "days_total": None,
                "days_elapsed": None}
    total = (end - start).days + 1
    elapsed = max(0, min(total, (today - start).days + 1))
    elapsed_percent = round(elapsed / total * 100, 2)
    if today < start:
        status = "NOT_STARTED"
    elif used is None:
        status = None
    elif used - elapsed_percent > PACE_TOLERANCE:
        status = "AHEAD"
    elif elapsed_percent - used > PACE_TOLERANCE:
        status = "BEHIND"
    else:
        status = "ON_TRACK"
    return {"time_elapsed_percent": elapsed_percent, "budget_used_percent": used, "status": status,
            "days_total": total, "days_elapsed": elapsed}


async def campaign_detail(initiative_id: UUID, date_from: date, date_to: date, today: date | None = None) -> dict:
    """One campaign for the side panel: its row (compared with its platform), trend, funnel, budget pace,
    CRM results and details."""
    today = today or date.today()
    pool = get_pool()
    async with pool.acquire() as conn:
        found = await _fetch_campaigns(conn, {}, [initiative_id])
        if not found:
            raise CampaignNotFoundError
        links = await conn.fetch(
            "SELECT system, external_id, account_id FROM initiative_links WHERE initiative_id = $1 ORDER BY system, external_id",
            initiative_id,
        )
    campaign = found[0]
    data = await _compute({"currency": campaign["currency"]}, date_from, date_to, today, include_ids=[initiative_id])
    row = next(r for r in data["rows"] if r["initiative_id"] == initiative_id)
    bucket, trend = _series([row], date_from, date_to)
    so_far = row["_to_date"]
    platform = next((p for p in data["by_platform"] if p["platform"] == campaign["platform"]), None)
    row.pop("_days")
    row.pop("_to_date")
    return {
        "campaign": row,
        "currency": campaign["currency"],
        "date_from": date_from, "date_to": date_to, "bucket": bucket, "trend": trend,
        "funnel": {k: row[k] for k in ("impressions", "clicks", "conversions", "verified_leads", "mqls", "sqls")},
        "platform_cpl": platform["cpl"] if platform else None,
        "budget": {
            "budget_amount": row["budget_amount"], "spent": row["spend_to_date"],
            "remaining": None if row["budget_amount"] is None else _money(row["budget_amount"] - (row["spend_to_date"] or 0)),
            **pace(row["budget_amount"], row["spend_to_date"], campaign["start_date"], campaign["end_date"], today),
        },
        "crm": {
            "verified_leads": so_far["verified_leads"], "mqls": so_far["mqls"], "sqls": so_far["sqls"],
            "demos": so_far["demos"], "customers": so_far["customers"], "revenue": _money(so_far["revenue"]),
            "spend": _money(so_far["spend"]),
            "cost_per_lead": ratio(so_far["spend"], so_far["verified_leads"]),
            "cost_per_sql": ratio(so_far["spend"], so_far["sqls"]),
            "roas": ratio(so_far["revenue"], so_far["spend"]),
        },
        "links": [dict(link) for link in links],
    }


async def health(filters: dict, today: date | None = None) -> dict:
    """Per platform: where the results come from (API connection, spreadsheet, typed in), the latest data date,
    the ad accounts, and the campaigns with missing or late data. Every currency (no money here)."""
    today = today or date.today()
    pool = get_pool()
    async with pool.acquire() as conn:
        campaigns = [c for c in await _fetch_campaigns(conn, filters) if c["status"] not in HIDDEN_STATUSES]
        ids = [c["initiative_id"] for c in campaigns]
        to_date = await _fetch_to_date(conn, ids, today) if ids else {}
        connections = await _fetch_connections(conn)
        syncs = await _fetch_ad_syncs(conn)

    platforms = []
    for platform in PLATFORMS:
        items = [c for c in campaigns if c["platform"] == platform]
        if not items:
            continue
        status = connection_status(platform, connections)
        problems, sources, last_dates, updated = [], defaultdict(int), [], []
        accounts = sorted({c["ad_account_id"] for c in items if c["ad_account_id"]})
        for c in items:
            so_far = to_date.get(c["initiative_id"], {})
            h = data_health(c, so_far.get("last_data_date"), so_far.get("latest_source"), status, today)
            if h["source"]:
                sources[h["source"]] += 1
            if h["last_data_date"]:
                last_dates.append(h["last_data_date"])
            if so_far.get("updated_at"):
                updated.append(so_far["updated_at"])
            if h["status"] in ("STALE", "NO_DATA", "CONNECTION"):
                problems.append({
                    "initiative_id": c["initiative_id"], "name": c["name"], "brand_name": c["brand_name"],
                    "status": c["status"], "health": h["status"], "last_data_date": h["last_data_date"],
                })
        problems.sort(key=lambda p: (p["last_data_date"] or date.min, p["name"].lower()))
        provider = CONNECTION_PROVIDERS.get(platform)
        sync = syncs.get(provider or "")
        platforms.append({
            "platform": platform,
            "connection_status": status,
            "connections": [
                {"account_name": c["account_name"], "account_id": c["provider_account_id"], "status": c["status"],
                 "updated_at": c["updated_at"]}
                for c in connections.get(provider or "", [])
            ],
            # The source most of its campaigns' latest results came from
            "source": "API" if status == "CONNECTED" else (max(sources, key=sources.get) if sources else None),
            "accounts": accounts,
            "campaign_count": len(items),
            "active_campaign_count": sum(1 for c in items if c["status"] == "ACTIVE"),
            "last_data_date": max(last_dates) if last_dates else None,
            "last_updated_at": max(updated) if updated else None,
            # The platform's sync, for the platforms one fills (Google Ads and YouTube Ads, Bing Ads)
            "last_sync_at": sync["last_success_at"] if sync else None,
            "sync_mode": sync["mode"] if sync else None,
            "problems": problems,
        })
    return {"today": today, "stale_after_days": STALE_DAYS, "platforms": platforms}
