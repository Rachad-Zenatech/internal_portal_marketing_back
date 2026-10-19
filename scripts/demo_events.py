#!/usr/bin/env python3
"""Demo events for the Events Overview: add or remove them. LOCAL DEVELOPMENT ONLY.

Usage:
    venv/Scripts/python scripts/demo_events.py add
    venv/Scripts/python scripts/demo_events.py remove

Refuses to run unless DATABASE_URL's host is localhost or 127.0.0.1.

add: 8 in-person events this year (August to December 2026) and the same 8 a year earlier, with costs in every
Events Overview cost category and results. Safe to re-run: an event that already exists is skipped, and costs and
results are only added to demo events that have none yet.

remove: deletes the demo events (their costs, results and Navan lines go with them) and nothing else.

How demo events are recognised: every name starts with "[Demo] " (services.navan_demo_service.DEMO_EVENT_PREFIX),
e.g. "[Demo] SaaS North Expo 2026". The same name a year apart ("... 2025") is how the Events Overview finds last
year's event. remove only deletes Events-channel events whose name is one of the names this script adds, and the
Navan demo mode (NAVAN_MODE=demo) only generates bookings for events whose name has the prefix.

Travel (flights, hotels, cars) is never added here: run the Navan sync with NAVAN_MODE=demo to add it.

Everything goes through the services (initiatives, costs, results, delete), so the usual rules apply and every
change has its audit entry, recorded as made by the owner of each event (an existing active user).
"""
import argparse
import asyncio
import os
import sys
from datetime import date, timedelta
from urllib.parse import urlparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models.initiative_model import EventDetailsInput, InitiativeCreate  # noqa: E402
from models.spend_model import SpendLineCreate  # noqa: E402
from postgresql_db.database import close_pool, create_pool, get_pool  # noqa: E402
from services.initiative_service import create_initiative, delete_initiative  # noqa: E402
from services.navan_demo_service import DEMO_EVENT_PREFIX  # noqa: E402
from services.performance_service import save_outcomes  # noqa: E402
from services.spend_service import create_spend_line  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1"}
THIS_YEAR = 2026
CURRENCY = "USD"

# One event a year: (name, brand, market, event type, venue, city, country, start, days, budget, travel budget, size,
#                    what's special). size scales costs and results (1.0 is about a $30,000 show).
# OVER_BUDGET: costs above the budget. NO_BUDGET: no budget at all. NO_LEADS: active, no leads recorded yet.
EVENTS = [
    ("SaaS North Expo", "DESKFLEX", "NORTH_AMERICA", "TRADE_SHOW", "Shaw Centre", "Ottawa", "CA",
     date(2026, 8, 11), 3, 52000, 14000, 1.0, None),
    ("Facilities Show London", "OTHERSHIP", "LONDON", "TRADE_SHOW", "ExCeL London", "London", "GB",
     date(2026, 8, 25), 3, 26000, 9000, 1.1, "OVER_BUDGET"),
    ("Workplace Innovation Summit", "WORKAWARE", "NORTH_AMERICA", "CONFERENCE", "McCormick Place", "Chicago", "US",
     date(2026, 9, 15), 2, 36000, 10000, 0.8, None),
    ("Drone Ops Forum", "K9SKY", "NORTH_AMERICA", "CONFERENCE", "Austin Convention Center", "Austin", "US",
     date(2026, 10, 6), 2, 22000, 8000, 0.9, "OVER_BUDGET"),
    ("Smart Building Week Berlin", "TILLERSTACK", "GERMANY", "TRADE_SHOW", "Messe Berlin", "Berlin", "DE",
     date(2026, 10, 15), 6, 48000, 15000, 1.2, "NO_LEADS"),
    ("Field Service Live Sydney", "PACE_PLUS", "AUSTRALIA", "ROADSHOW", "ICC Sydney", "Sydney", "AU",
     date(2026, 11, 10), 3, 40000, 16000, 0.9, None),
    ("Hybrid Work Roadshow Seoul", "ZOO_OFFICE", "KOREA", "ROADSHOW", "COEX", "Seoul", "KR",
     date(2026, 11, 24), 2, None, None, 0.6, "NO_BUDGET"),
    ("Partner Connect Toronto", "JADIAN", "NORTH_AMERICA", "PARTNER_EVENT", "Metro Toronto Convention Centre",
     "Toronto", "CA", date(2026, 12, 8), 2, 30000, 9000, 0.7, None),
]

# Last year's events: same weekday a year earlier, lower costs and results
PRIOR_YEAR_DAYS = 364
PRIOR_YEAR_COSTS = 0.85
PRIOR_YEAR_RESULTS = 0.8

# Costs of a size-1.0 event, not travel: (category, description, vendor, amount, days from the start, source).
# Booth space and show services, materials, staff time, meals and per diems, ground transport and other.
COSTS = [
    ("BOOTH_SPACE", "Booth space, 6 x 6 m", "Show organiser", 9000, -120, "MANUAL"),
    ("BOOTH_BUILD", "Booth build and rental", "Exhibit Works", 6500, -30, "MANUAL"),
    ("ELECTRICAL", "Booth electrical", "Show services", 900, -14, "MANUAL"),
    ("FLOORING", "Carpet and flooring", "Show services", 700, -14, "MANUAL"),
    ("FREIGHT", "Freight to and from the venue", "Logistics partner", 1800, -10, "MANUAL"),
    ("PRINT_SIGNAGE", "Banners and brochures", "PrintCo", 1500, -21, "MANUAL"),
    ("GIVEAWAYS", "Branded giveaways", "Swag Supply", 1200, -25, "MANUAL"),
    ("STAFF_TIME", "Staff time (staff calculator)", None, 4800, 0, "STAFF_CALCULATOR"),
    ("PER_DIEMS_MEALS", "Per diems for the team", None, 1100, 0, "MANUAL"),
    ("PER_DIEMS_MEALS", "Team dinner", "Restaurant", 650, 1, "MANUAL"),
    ("GROUND_TRANSPORT", "Uber and taxis", "Uber", 420, 0, "MANUAL"),
    ("LEAD_RETRIEVAL", "Lead retrieval app", "Show organiser", 450, -7, "MANUAL"),
    ("CLIENT_HOSPITALITY", "Customer drinks reception", "Venue catering", 1600, 1, "MANUAL"),
]
# Costs that are invoiced late, so an event that finished in the last LATE_INVOICE_DAYS still has some awaiting cost
LATE_INVOICES = {"CLIENT_HOSPITALITY"}
LATE_INVOICE_DAYS = 60
# Paid well before the event (deposits): actual even for planned events
PAID_EARLY = {"BOOTH_SPACE"}
# Committed by now for an upcoming event (order placed); the rest are still estimates
COMMITTED_EARLY = {"BOOTH_BUILD", "FREIGHT", "PRINT_SIGNAGE", "LEAD_RETRIEVAL"}
# Bought by the time an event is under way
INVOICED_WHILE_ACTIVE = {"BOOTH_BUILD", "ELECTRICAL", "FLOORING", "FREIGHT", "PRINT_SIGNAGE", "GIVEAWAYS",
                         "STAFF_TIME", "PER_DIEMS_MEALS", "GROUND_TRANSPORT"}

# Results of a size-1.0 event: visitors, then each step of the funnel as a share of the one before.
FUNNEL = {"VISITORS": 1400, "LEADS": 0.16, "MQLS": 0.38, "DEMOS": 0.45, "CUSTOMERS": 0.22}
DEAL_SIZE = 14500  # revenue per customer, in the event's currency


def _require_local_database() -> None:
    host = urlparse(os.environ.get("DATABASE_URL", "")).hostname
    # Another database only when its host is named on purpose (scripts/setup_prod_db.py --sample-data)
    if host not in LOCAL_HOSTS and not (host and host == os.environ.get("ALLOW_SAMPLE_DATA_HOST")):
        sys.exit("Refusing to run: DATABASE_URL is not a local database (localhost or 127.0.0.1).")


def _instances() -> list[dict]:
    """Every demo event to add: this year's and last year's."""
    out = []
    for name, brand, market, event_type, venue, city, country, start, days, budget, travel, size, flag in EVENTS:
        for prior in (False, True):
            first = start - timedelta(days=PRIOR_YEAR_DAYS) if prior else start
            year = THIS_YEAR - 1 if prior else THIS_YEAR
            out.append({
                "name": f"{DEMO_EVENT_PREFIX}{name} {year}", "brand": brand, "market": market,
                "event_type": event_type, "venue": venue, "city": city, "country": country,
                "start": first, "end": first + timedelta(days=days - 1),
                # Last year's events all had a budget
                "budget": budget if not prior else round((budget or 30000) * PRIOR_YEAR_COSTS, -2),
                "travel": travel if not prior else round((travel or 9000) * PRIOR_YEAR_COSTS, -2),
                "cost_scale": size * (PRIOR_YEAR_COSTS if prior else 1),
                "result_scale": size * (PRIOR_YEAR_RESULTS if prior else 1),
                "flag": None if prior else flag,
            })
    return out


def demo_event_names() -> list[str]:
    return [e["name"] for e in _instances()]


def _status(start: date, end: date, today: date) -> str:
    if start > today:
        return "PLANNED"
    return "COMPLETED" if end < today else "ACTIVE"


def _cost_status(category: str, timing: str, recent: bool) -> str:
    if timing == "COMPLETED":
        return "COMMITTED" if category in LATE_INVOICES and recent else "PAID"
    if timing == "ACTIVE":
        if category in PAID_EARLY:
            return "PAID"
        return "INVOICED" if category in INVOICED_WHILE_ACTIVE else "COMMITTED"
    if category in PAID_EARLY:
        return "PAID"
    return "COMMITTED" if category in COMMITTED_EARLY else "ESTIMATED"


async def _lookups(conn) -> dict:
    async def codes(table: str) -> dict:
        return {r["code"]: r["id"] for r in await conn.fetch(f"SELECT id, code FROM {table}")}

    owners = [r["id"] for r in await conn.fetch(
        "SELECT id FROM users WHERE is_active AND deleted_at IS NULL ORDER BY is_super_admin DESC, created_at"
    )]
    if not owners:
        sys.exit("No active user to own the demo events. Sign in to the portal once first.")
    return {
        "brands": await codes("brands"), "markets": await codes("markets"), "event_types": await codes("event_types"),
        "categories": await codes("spend_categories"),
        "channel": await conn.fetchval("SELECT id FROM channels WHERE code = 'EVENTS'"),
        "sub_channel": await conn.fetchval("SELECT id FROM sub_channels WHERE code = 'IN_PERSON_EVENTS'"),
        "owners": owners,
    }


def _money(value: float) -> float:
    return round(value, 2)


async def _add_costs(event: dict, initiative_id, refs: dict, owner, timing: str, today: date) -> int:
    scale = event["cost_scale"]
    # Over budget: the costs grow past the (small) budget; the small spread keeps events from looking identical
    if event["flag"] == "OVER_BUDGET":
        scale *= 1.25
    spread = 0.9 + (sum(map(ord, event["name"])) % 21) / 100
    recent = event["end"] >= today - timedelta(days=LATE_INVOICE_DAYS)
    added = 0
    for category, description, vendor, amount, offset, source in COSTS:
        status = _cost_status(category, timing, recent)
        incurred = event["start"] + timedelta(days=offset)
        paid_on = None
        if status in ("INVOICED", "PAID"):
            incurred = min(incurred, today)
            if status == "PAID":
                paid_on = min(incurred + timedelta(days=14), today)
        line = SpendLineCreate(
            category_id=refs["categories"][category], description=description, vendor=vendor, status=status,
            amount=_money(amount * scale * spread), incurred_on=incurred, paid_on=paid_on,
            payment_method="AP_INVOICE" if status == "PAID" else None, source=source,
        )
        await create_spend_line(initiative_id, line, owner)
        added += 1
    return added


async def _add_results(event: dict, initiative_id, owner, timing: str) -> bool:
    if timing == "PLANNED":
        return False
    scale = event["result_scale"] * (0.85 + (sum(map(ord, event["name"])) % 31) / 100)
    counts = {"VISITORS": round(FUNNEL["VISITORS"] * scale)}
    previous = counts["VISITORS"]
    for code in ("LEADS", "MQLS", "DEMOS", "CUSTOMERS"):
        previous = max(1, round(previous * FUNNEL[code]))
        counts[code] = previous
    if event["flag"] == "NO_LEADS" or timing == "ACTIVE":
        # Under way: visitors counted at the booth, no leads entered yet
        values = [{"metric_code": "VISITORS", "value": counts["VISITORS"], "currency": None}]
    else:
        values = [{"metric_code": code, "value": value, "currency": None} for code, value in counts.items()]
        values.append({"metric_code": "REVENUE", "value": counts["CUSTOMERS"] * DEAL_SIZE, "currency": CURRENCY})
    await save_outcomes(initiative_id, event["start"].replace(day=1), values, owner)
    return True


async def add() -> None:
    today = date.today()
    pool = get_pool()
    async with pool.acquire() as conn:
        refs = await _lookups(conn)
        existing = {r["name"]: r["id"] for r in await conn.fetch(
            "SELECT id, name FROM initiatives WHERE channel_id = $1 AND name = ANY($2::text[])",
            refs["channel"], demo_event_names(),
        )}
    created = costs = results = 0
    for n, event in enumerate(_instances()):
        owner = refs["owners"][n % len(refs["owners"])]
        timing = _status(event["start"], event["end"], today)
        initiative_id = existing.get(event["name"])
        if initiative_id is None:
            row = await create_initiative(InitiativeCreate(
                name=event["name"], brand_id=refs["brands"][event["brand"]], channel_id=refs["channel"],
                sub_channel_id=refs["sub_channel"], market_id=refs["markets"][event["market"]], owner_user_id=owner,
                start_date=event["start"], end_date=event["end"], budget_amount=event["budget"] or 0, currency=CURRENCY,
                status=timing,
                event=EventDetailsInput(
                    event_type_id=refs["event_types"][event["event_type"]], venue=event["venue"],
                    city=event["city"], country_code=event["country"], travel_budget_amount=event["travel"],
                ),
            ), owner)
            initiative_id = row["id"]
            created += 1
            if event["budget"] is None:
                # New initiatives need a budget now; this one shows the "No budget" flag that older initiatives get
                async with pool.acquire() as conn:
                    await conn.execute("UPDATE initiatives SET budget_amount = NULL WHERE id = $1", initiative_id)
        async with pool.acquire() as conn:
            has_costs = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM spend_lines WHERE initiative_id = $1 AND source <> 'NAVAN')", initiative_id
            )
            has_results = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM outcomes WHERE initiative_id = $1)", initiative_id)
        if not has_costs:
            costs += await _add_costs(event, initiative_id, refs, owner, timing, today)
        if not has_results and await _add_results(event, initiative_id, owner, timing):
            results += 1
    print(f"Added {created} demo event(s), {costs} cost(s) and results for {results} event(s).")
    print("Travel comes from the Navan demo mode: run the Navan sync with NAVAN_MODE=demo.")


async def remove() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT i.id, i.owner_user_id FROM initiatives i JOIN channels c ON c.id = i.channel_id AND c.code = 'EVENTS'
            WHERE i.name = ANY($1::text[])
            """,
            demo_event_names(),
        )
        fallback = await conn.fetchval(
            "SELECT id FROM users WHERE is_active AND deleted_at IS NULL ORDER BY is_super_admin DESC, created_at LIMIT 1"
        )
    for row in rows:
        await delete_initiative(row["id"], row["owner_user_id"] or fallback)
    print(f"Removed {len(rows)} demo event(s).")


async def main(command: str) -> None:
    _require_local_database()
    await create_pool()
    try:
        await (add() if command == "add" else remove())
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add or remove the demo events (local development only).")
    parser.add_argument("command", choices=["add", "remove"])
    asyncio.run(main(parser.parse_args().command))
