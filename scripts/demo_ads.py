#!/usr/bin/env python3
"""Demo campaigns for the Paid Advertising page: add or remove them. LOCAL DEVELOPMENT ONLY.

Usage:
    venv/Scripts/python scripts/demo_ads.py add
    venv/Scripts/python scripts/demo_ads.py remove

Refuses to run unless DATABASE_URL's host is localhost or 127.0.0.1.

add: 24 ad campaigns on Google Ads, Microsoft Ads (Bing), LinkedIn Ads, Meta Ads, YouTube Ads and TikTok Ads for
DeskFlex, WorkAware, Othership and Tillerstack, with weekly results over the last 6 months (spend, impressions,
clicks, platform conversions, verified leads, MQLs and SQLs), some customers and revenue, primary KPIs with targets
and a few Google Ads campaign links. Dates are relative to today, so the last 30 days always look alike.
Every "Campaigns needing attention" rule has a case: budget nearly used up, cost per lead above target, paused while
it should be running, and data delayed. Safe to re-run: a campaign that already exists is skipped, and results are
only added to demo campaigns that have none yet. To move the dates up to today, run remove and then add.

remove: deletes the demo campaigns (their results, costs, outcomes and links go with them) and nothing else.

How demo campaigns are recognised: every name starts with "[Demo] " and remove only deletes Paid Search and Paid
Social initiatives whose name is one of the names this script adds.

Everything goes through the services (initiatives, ad results, outcomes, delete), so the usual rules apply and every
change has its audit entry, recorded as made by the campaign's owner (an existing active user).
"""
import argparse
import asyncio
import os
import random
import sys
import zlib
from datetime import date, timedelta
from urllib.parse import urlparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models.initiative_model import InitiativeCreate, InitiativeLinkInput  # noqa: E402
from postgresql_db.database import close_pool, create_pool, get_pool  # noqa: E402
from services.shared import ads_service
from services.shared.initiative_service import create_initiative, delete_initiative  # noqa: E402
from services.performance.performance_service import save_outcomes  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1"}
PREFIX = "[Demo] "
CURRENCY = "USD"
HISTORY_DAYS = 182

# Per platform: channel, cost per click, click-through rate, cost per verified lead, platform conversions per lead,
# result source (spreadsheet upload for search, typed in for social), ad account id
PLATFORMS = {
    "GOOGLE_ADS": ("PAID_SEARCH", 3.10, 0.046, 66, 1.6, "SPREADSHEET", "482-913-2207"),
    "BING_ADS": ("PAID_SEARCH", 2.30, 0.039, 58, 1.5, "SPREADSHEET", "F118-2290"),
    "LINKEDIN_ADS": ("PAID_SOCIAL", 7.40, 0.0075, 114, 1.3, "MANUAL", "508812734"),
    "META_ADS": ("PAID_SOCIAL", 1.30, 0.012, 68, 1.8, "MANUAL", "act_2210938476"),
    "YOUTUBE_ADS": ("PAID_SOCIAL", 0.65, 0.007, 102, 1.2, "MANUAL", "482-913-2207"),
    "TIKTOK_ADS": ("PAID_SOCIAL", 0.95, 0.011, 89, 1.4, "MANUAL", "7290018834"),
}
# Verified leads that become MQLs, MQLs that become SQLs, SQLs that become customers; revenue per customer
MQL_RATE, SQL_RATE, CUSTOMER_RATE = 0.42, 0.36, 0.22
DEAL_SIZE = {"DESKFLEX": 9000, "WORKAWARE": 6500, "OTHERSHIP": 7500, "TILLERSTACK": 12000}

# (name, platform, brand, market, objective, status, starts (days from today), days long, budget, pace,
#  cost per lead factor, primary KPI (code, target) or None, special case, Google Ads campaign id or None,
#  short description)
# pace: spend per day as a share of budget / days. Special cases: BUDGET (94% of the budget spent), CPL_HIGH (cost
# per lead well above its target), PAUSED (paused 18 days ago, dates say running), STALE (no results for 12 days),
# CRM (customers and revenue recorded).
CAMPAIGNS = [
    ("DeskFlex - Desk Booking Software - NA", "GOOGLE_ADS", "DESKFLEX", "NORTH_AMERICA", "LEADS", "ACTIVE",
     -60, 90, 25000, 0.84, 1.0, ("COST_PER_LEAD", 70), "CRM", "7301245581", "Search ads for desk booking keywords."),
    ("DeskFlex - Hot Desking Brand Terms", "GOOGLE_ADS", "DESKFLEX", "NORTH_AMERICA", "CONVERSIONS", "ACTIVE",
     -50, 85, 22000, 0.82, 0.95, ("CPC", 3.5), None, "7301245582", "Brand and competitor terms for hot desking."),
    ("WorkAware - Lone Worker Safety App", "GOOGLE_ADS", "WORKAWARE", "AUSTRALIA", "LEADS", "ACTIVE",
     -55, 90, 24000, 0.84, 1.05, ("COST_PER_LEAD", 75), "CRM", "7301245583", "Lone worker app searches in Australia."),
    ("Othership - Room Booking - UK", "GOOGLE_ADS", "OTHERSHIP", "LONDON", "TRAFFIC", "ACTIVE",
     -40, 80, 18000, 0.8, 1.0, None, None, None, "Meeting room booking traffic to the UK site."),
    ("Tillerstack - Field Service Software DE", "GOOGLE_ADS", "TILLERSTACK", "GERMANY", "LEADS", "ACTIVE",
     -70, 80, 20000, 1.06, 1.0, None, "BUDGET", "7301245585", "Field service software searches in Germany."),
    ("DeskFlex - Visitor Management - Bing", "BING_ADS", "DESKFLEX", "NORTH_AMERICA", "LEADS", "ACTIVE",
     -45, 80, 12000, 0.8, 1.0, None, None, None, "Visitor management keywords on Microsoft Ads."),
    ("WorkAware - Workplace Safety - Bing", "BING_ADS", "WORKAWARE", "NORTH_AMERICA", "LEADS", "ACTIVE",
     -35, 75, 10000, 0.85, 0.95, None, None, None, "Workplace safety keywords on Microsoft Ads."),
    ("Tillerstack - Job Scheduling - Bing", "BING_ADS", "TILLERSTACK", "GERMANY", "CONVERSIONS", "ACTIVE",
     -30, 70, 5000, 0.8, 1.0, None, None, None, "Job scheduling software on Microsoft Ads."),
    ("DeskFlex - Facilities Managers - LinkedIn", "LINKEDIN_ADS", "DESKFLEX", "NORTH_AMERICA", "LEADS", "ACTIVE",
     -55, 90, 25000, 0.82, 1.04, ("COST_PER_LEAD", 70), "CPL_HIGH", None, "Lead gen forms for facilities managers."),
    ("Othership - HR Leaders - LinkedIn", "LINKEDIN_ADS", "OTHERSHIP", "LONDON", "LEADS", "ACTIVE",
     -45, 85, 20000, 0.8, 0.96, ("COST_PER_LEAD", 120), None, None, "Sponsored content for HR leaders in the UK."),
    ("WorkAware - EHS Managers - LinkedIn", "LINKEDIN_ADS", "WORKAWARE", "AUSTRALIA", "ENGAGEMENT", "ACTIVE",
     -35, 80, 17000, 0.8, 1.08, None, None, None, "Thought leadership for EHS managers."),
    ("DeskFlex - Hybrid Work Retargeting - Meta", "META_ADS", "DESKFLEX", "NORTH_AMERICA", "CONVERSIONS", "ACTIVE",
     -45, 85, 19000, 0.84, 1.0, None, "CRM", None, "Retargeting site visitors with trial offers."),
    ("Othership - Office Booking - Meta", "META_ADS", "OTHERSHIP", "LONDON", "LEADS", "ACTIVE",
     -40, 80, 15000, 0.8, 0.95, ("COST_PER_MQL", 180), None, None, "Lead ads for small office managers."),
    ("Tillerstack - Trades Lead Gen - Meta", "META_ADS", "TILLERSTACK", "GERMANY", "LEADS", "ACTIVE",
     -50, 85, 13000, 0.82, 1.05, None, "STALE", None, "Lead ads for trades businesses."),
    ("WorkAware - Safety Stories - YouTube", "YOUTUBE_ADS", "WORKAWARE", "NORTH_AMERICA", "AWARENESS", "ACTIVE",
     -45, 85, 19000, 0.78, 1.0, None, None, None, "In-stream video with customer safety stories."),
    ("DeskFlex - Product Demo Videos - YouTube", "YOUTUBE_ADS", "DESKFLEX", "NORTH_AMERICA", "VIDEO_VIEWS", "ACTIVE",
     -30, 75, 12000, 0.8, 1.1, None, None, None, "Short product demo videos."),
    ("Othership - Gen Z Workspace - TikTok", "TIKTOK_ADS", "OTHERSHIP", "LONDON", "AWARENESS", "ACTIVE",
     -28, 70, 11000, 0.82, 1.0, None, None, None, "Creator videos about flexible workspaces."),
    ("Tillerstack - Apprentice Trades - TikTok", "TIKTOK_ADS", "TILLERSTACK", "AUSTRALIA", "TRAFFIC", "ACTIVE",
     -35, 75, 9000, 0.8, 1.1, ("CPC", 1.2), None, None, "Traffic campaign for young trades businesses."),
    ("WorkAware - Incident Reporting - Google", "GOOGLE_ADS", "WORKAWARE", "LONDON", "LEADS", "PAUSED",
     -50, 95, 12000, 0.85, 1.0, None, "PAUSED", "7301245599", "Incident reporting software searches in the UK."),
    ("DeskFlex - Holiday Office Planning - Meta", "META_ADS", "DESKFLEX", "NORTH_AMERICA", "AWARENESS", "PLANNED",
     12, 60, 7000, 0, 1.0, None, None, None, "Seasonal awareness push for office planning."),
    ("Tillerstack - 2027 Kickoff - LinkedIn", "LINKEDIN_ADS", "TILLERSTACK", "GERMANY", "LEADS", "PLANNED",
     40, 90, 15000, 0, 1.0, ("COST_PER_LEAD", 110), None, None, "New year lead gen for service managers."),
    ("Othership - Spring Workspace Push - Google", "GOOGLE_ADS", "OTHERSHIP", "LONDON", "LEADS", "COMPLETED",
     -182, 120, 16000, 0.95, 1.0, ("COST_PER_LEAD", 65), "CRM", "7301245601", "Spring search push for workspace booking."),
    ("WorkAware - Compliance Webinar - LinkedIn", "LINKEDIN_ADS", "WORKAWARE", "NORTH_AMERICA", "LEADS", "COMPLETED",
     -150, 115, 13000, 0.95, 1.0, None, "CRM", None, "Webinar sign ups for compliance teams."),
    ("DeskFlex - Q2 Brand Awareness - YouTube", "YOUTUBE_ADS", "DESKFLEX", "AUSTRALIA", "AWARENESS", "COMPLETED",
     -140, 110, 10000, 0.92, 1.0, None, None, None, "Brand video for the Australian launch."),
]


def _require_local_database() -> None:
    host = urlparse(os.environ.get("DATABASE_URL", "")).hostname
    # Another database only when its host is named on purpose (scripts/setup_prod_db.py --sample-data)
    if host not in LOCAL_HOSTS and not (host and host == os.environ.get("ALLOW_SAMPLE_DATA_HOST")):
        sys.exit("Refusing to run: DATABASE_URL is not a local database (localhost or 127.0.0.1).")


def demo_campaign_names() -> list[str]:
    return [PREFIX + c[0] for c in CAMPAIGNS]


def _spec(entry: tuple, today: date) -> dict:
    (name, platform, brand, market, objective, status, starts, days, budget, pace, cpl_factor, kpi, special,
     google_id, description) = entry
    start = today + timedelta(days=starts)
    return {
        "name": PREFIX + name, "platform": platform, "brand": brand, "market": market, "objective": objective,
        "status": status, "start": start, "end": start + timedelta(days=days - 1), "budget": budget, "pace": pace,
        "cpl_factor": cpl_factor, "kpi": kpi, "special": special, "google_id": google_id, "description": description,
    }


def _data_until(spec: dict, today: date) -> date | None:
    """The last day with results."""
    if spec["status"] == "PLANNED":
        return None
    if spec["special"] == "PAUSED":
        return today - timedelta(days=18)
    if spec["special"] == "STALE":
        return today - timedelta(days=12)
    return min(spec["end"], today)


def weekly_results(spec: dict, today: date) -> list[dict]:
    """Monday-to-Sunday results from the campaign's start (at most 6 months back) to its last day with results."""
    until = _data_until(spec, today)
    if until is None or until < spec["start"]:
        return []
    channel, cpc, ctr, cpl, conv_per_lead, _source, _account = PLATFORMS[spec["platform"]]
    rng = random.Random(zlib.crc32(spec["name"].encode()))
    total_days = (spec["end"] - spec["start"]).days + 1
    per_day = spec["budget"] / total_days
    first = max(spec["start"], today - timedelta(days=HISTORY_DAYS))
    week = first - timedelta(days=first.weekday())
    rows = []
    while week <= until:
        start = max(week, first)
        last = min(week + timedelta(days=6), until)
        days = (last - start).days + 1
        ramp = 0.7 if (start - spec["start"]).days < 7 else 1.0
        spend = per_day * days * spec["pace"] * ramp * rng.uniform(0.88, 1.12)
        clicks = spend / (cpc * rng.uniform(0.9, 1.1))
        impressions = clicks / (ctr * rng.uniform(0.9, 1.1))
        leads = spend / (cpl * spec["cpl_factor"] * rng.uniform(0.9, 1.1))
        rows.append({
            "period_start": start, "period_end": min(week + timedelta(days=6), spec["end"]),
            "budget_amount": round(per_day * days, 2), "spend": spend, "impressions": impressions,
            "clicks": clicks, "leads": leads, "conv_per_lead": conv_per_lead * rng.uniform(0.85, 1.15),
        })
        week += timedelta(days=7)
    if spec["special"] == "BUDGET":
        # 94% of the budget spent so far
        scale = spec["budget"] * 0.94 / sum(r["spend"] for r in rows)
        for r in rows:
            r["spend"] *= scale
            r["clicks"] *= scale
            r["impressions"] *= scale
            r["leads"] *= scale
    out = []
    for n, r in enumerate(rows):
        leads = round(r["leads"])
        mqls = round(leads * MQL_RATE)
        last_row = n == len(rows) - 1
        status = "ACTIVE"
        if last_row and spec["status"] == "COMPLETED":
            status = "ENDED"
        elif last_row and spec["status"] == "PAUSED":
            status = "PAUSED"
        out.append({
            "ad_group": None, "period_start": r["period_start"], "period_end": r["period_end"],
            "budget_amount": r["budget_amount"], "spend": round(r["spend"], 2), "impressions": round(r["impressions"]),
            "clicks": round(r["clicks"]), "conversions": round(leads * r["conv_per_lead"]), "hubspot_contacts": leads,
            "mqls": mqls, "sqls": round(mqls * SQL_RATE), "status": status, "report_url": None, "tracking_detail": None,
        })
    if out and spec["google_id"]:
        out[-1]["report_url"] = f"https://lookerstudio.google.com/reporting/demo-{spec['google_id']}"
    return out


async def _lookups(conn) -> dict:
    async def codes(table: str) -> dict:
        return {r["code"]: r["id"] for r in await conn.fetch(f"SELECT id, code FROM {table}")}

    owners = [r["id"] for r in await conn.fetch(
        "SELECT id FROM users WHERE is_active AND deleted_at IS NULL ORDER BY is_super_admin DESC, created_at"
    )]
    if not owners:
        sys.exit("No active user to own the demo campaigns. Sign in to the portal once first.")
    return {
        "brands": await codes("brands"), "markets": await codes("markets"), "channels": await codes("channels"),
        "sub_channels": await codes("sub_channels"), "kpis": await codes("primary_kpis"), "owners": owners,
    }


async def _add_results(spec: dict, initiative_id, owner, today: date) -> int:
    rows = weekly_results(spec, today)
    if not rows:
        return 0
    source = PLATFORMS[spec["platform"]][5]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ads_service.apply_results(conn, [{**r, "initiative_id": initiative_id} for r in rows], source, owner)
            await ads_service.sync_initiatives(conn, [initiative_id], owner)
    if spec["special"] == "CRM":
        # Customers and revenue as Sales records them, per month
        by_month: dict[date, int] = {}
        for r in rows:
            month = r["period_start"].replace(day=1)
            by_month[month] = by_month.get(month, 0) + r["sqls"]
        for month, sqls in sorted(by_month.items()):
            customers = round(sqls * CUSTOMER_RATE)
            if customers:
                await save_outcomes(initiative_id, month, [
                    {"metric_code": "CUSTOMERS", "value": customers, "currency": None},
                    {"metric_code": "REVENUE", "value": customers * DEAL_SIZE[spec["brand"]], "currency": CURRENCY},
                ], owner)
    return len(rows)


async def add() -> None:
    today = date.today()
    pool = get_pool()
    async with pool.acquire() as conn:
        refs = await _lookups(conn)
        existing = {r["name"]: r["id"] for r in await conn.fetch(
            f"SELECT i.id, i.name {ads_service.CAMPAIGNS_FROM} WHERE i.name = ANY($1::text[])", demo_campaign_names()
        )}
    created = results = 0
    for n, entry in enumerate(CAMPAIGNS):
        spec = _spec(entry, today)
        owner = refs["owners"][n % len(refs["owners"])]
        channel, *_rest, account = PLATFORMS[spec["platform"]]
        initiative_id = existing.get(spec["name"])
        if initiative_id is None:
            kpi_code, target = spec["kpi"] or (None, None)
            row = await create_initiative(InitiativeCreate(
                name=spec["name"], brand_id=refs["brands"][spec["brand"]], channel_id=refs["channels"][channel],
                sub_channel_id=refs["sub_channels"][spec["platform"]], market_id=refs["markets"][spec["market"]],
                owner_user_id=owner, start_date=spec["start"], end_date=spec["end"], budget_amount=spec["budget"],
                currency=CURRENCY, status=spec["status"], description=spec["description"],
                primary_kpi_id=refs["kpis"][kpi_code] if kpi_code else None, kpi_target=target,
                data_source="LINKED" if spec["google_id"] else "MANUAL",
                channel_details={"campaign_objective": spec["objective"], "ad_account_id": account},
                links=[InitiativeLinkInput(system="GOOGLE_ADS", external_id=spec["google_id"], account_id=account)]
                if spec["google_id"] else [],
            ), owner)
            initiative_id = row["id"]
            created += 1
        async with pool.acquire() as conn:
            has_results = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM ad_results WHERE initiative_id = $1)", initiative_id)
        if not has_results:
            results += await _add_results(spec, initiative_id, owner, today)
    print(f"Added {created} demo campaign(s) and {results} weekly result(s).")


async def remove() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT i.id, i.owner_user_id {ads_service.CAMPAIGNS_FROM} WHERE i.name = ANY($1::text[])",
            demo_campaign_names(),
        )
        fallback = await conn.fetchval(
            "SELECT id FROM users WHERE is_active AND deleted_at IS NULL ORDER BY is_super_admin DESC, created_at LIMIT 1"
        )
    for row in rows:
        await delete_initiative(row["id"], row["owner_user_id"] or fallback)
    print(f"Removed {len(rows)} demo campaign(s).")


async def main(command: str) -> None:
    _require_local_database()
    await create_pool()
    try:
        await (add() if command == "add" else remove())
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add or remove the demo ad campaigns (local development only).")
    parser.add_argument("command", choices=["add", "remove"])
    asyncio.run(main(parser.parse_args().command))
