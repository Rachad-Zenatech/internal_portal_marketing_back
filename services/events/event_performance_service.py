"""Events Overview: budget, actual spend, prior year spend, results and ROI of events, per event and per currency.

Calculated on request, never stored:
- Actual cost counts only invoiced and paid cost lines, in the event's currency; estimates and commitments are
  awaiting cost. Lines in other currencies are counted in other_currency_lines, never converted.
- Actual cost is split into seven categories (COST_CATEGORIES) for the cost breakdown.
- Results are summed across months; a synced value (HubSpot...) beats a typed-in one for the same month and result.
- An event with a demo log counts its demos from the log (demos that have happened), not typed-in numbers.
- Revenue is in the event's currency. ROI = (revenue - actual cost) / actual cost.
- Money is never added across currencies: totals, monthly rows and the cost breakdown are per currency.

Every window (the period, the previous period and both a year earlier) is read with the same set-based query,
never one query per event.
"""
from datetime import date, timedelta
from uuid import UUID

from postgresql_db.database import get_pool
from services.shared import initiative_metrics as metrics
from services.shared.demo_service import HELD_DEMO_SQL
from services.shared.travel_cost_rules import travel_sql

EVENTS_CHANNEL = "EVENTS"

# The seven categories of the cost breakdown. Travel comes first (services/shared/travel_cost_rules.py) and is split into
# travel, ground transport and meals & per diems by category; then category overrides, then the expense type.
# Anything else is Other (sponsorship fees, hospitality, equipment, agency fees, software...).
TRAVEL_CATEGORY_SPLIT = {"GROUND_TRANSPORT": "GROUND_TRANSPORT", "PER_DIEMS_MEALS": "MEALS_PER_DIEMS"}
CATEGORY_OVERRIDES = {
    "BOOTH_SPACE": "BOOTH_SPACE", "BADGES_PASSES": "BOOTH_SPACE", "STAFF_TIME": "STAFF_TIME",
    "DEMO_EQUIPMENT": "OTHER", "LEAD_RETRIEVAL": "OTHER", "AV_TECH": "OTHER",
}
EXPENSE_TYPE_CATEGORIES = {
    "BOOTH_EXHIBIT": "BOOTH_SPACE", "SHOW_SERVICES": "BOOTH_SPACE", "SHIPPING_LOGISTICS": "BOOTH_SPACE",
    "MARKETING_PROMOTION": "MATERIALS", "MEDIA": "MATERIALS",
}
COST_CATEGORIES = ("BOOTH_SPACE", "TRAVEL", "GROUND_TRANSPORT", "STAFF_TIME", "MEALS_PER_DIEMS", "MATERIALS", "OTHER")

# Cost lines that count as actual spend: the invoice is in
ACTUAL_STATUSES = ("INVOICED", "PAID")

# Result counts on the overview, by outcome metric. Attendees are visitors and qualified leads are MQLs.
COUNTS = {"attendees": "VISITORS", "leads": "LEADS", "qualified_leads": "MQLS", "demos": "DEMOS", "customers": "CUSTOMERS"}

# Last year's event: started 300 to 430 days before this one, closest to exactly a year earlier wins
PRIOR_YEAR_MIN_DAYS = 300
PRIOR_YEAR_MAX_DAYS = 430


def _sql_case(column: str, mapping: dict[str, str]) -> str:
    return " ".join(f"WHEN {column} = '{code}' THEN '{category}'" for code, category in mapping.items())


def _category_case() -> str:
    travel_split = _sql_case("sc.code", TRAVEL_CATEGORY_SPLIT)
    return (
        f"CASE WHEN {travel_sql('s')} THEN (CASE {travel_split} ELSE 'TRAVEL' END) "
        f"{_sql_case('sc.code', CATEGORY_OVERRIDES)} {_sql_case('et.code', EXPENSE_TYPE_CATEGORIES)} ELSE 'OTHER' END"
    )


def name_key_sql(column: str) -> str:
    """An event's name for matching last year's event: lower case, trimmed, without a year from 2000 to 2099
    (a standalone 4-digit number), and with runs of spaces made one, so "SaaS North Expo 2025" matches
    "SaaS North Expo 2026"."""
    without_year = f"regexp_replace({column}, '(?<![0-9])20[0-9]{{2}}(?![0-9])', '', 'g')"
    return f"lower(btrim(regexp_replace({without_year}, '\\s+', ' ', 'g')))"


# No market counts as Unassigned, as it does for budgets and the initiatives list
_MARKET_SQL = "COALESCE(i.market_id, (SELECT id FROM markets WHERE code = 'UNASSIGNED'))"

# $1, $2: the window (either may be null for no limit); $3..$7: brand, market, event type, owner, status
_EVENTS_SQL = f"""
    WITH ev AS (
        SELECT i.id, i.name, ed.event_code, b.name AS brand_name, mk.name AS market_name,
               ed.city, ed.country_code, evt.name AS event_type_name, u.full_name AS owner_name,
               i.start_date, i.end_date, i.status, i.currency, i.budget_amount::float8 AS budget_amount
        FROM initiatives i
        JOIN channels c ON c.id = i.channel_id AND c.code = '{EVENTS_CHANNEL}'
        JOIN brands b ON b.id = i.brand_id
        LEFT JOIN markets mk ON mk.id = {_MARKET_SQL}
        LEFT JOIN event_details ed ON ed.initiative_id = i.id
        LEFT JOIN event_types evt ON evt.id = ed.event_type_id
        LEFT JOIN users u ON u.id = i.owner_user_id
        WHERE i.status <> 'DRAFT'
          AND i.start_date IS NOT NULL
          AND ($1::date IS NULL OR COALESCE(i.end_date, i.start_date) >= $1::date)
          AND ($2::date IS NULL OR i.start_date <= $2::date)
          AND ($3::int IS NULL OR i.brand_id = $3::int)
          AND ($4::int IS NULL OR {_MARKET_SQL} = $4::int)
          AND ($5::int IS NULL OR ed.event_type_id = $5::int)
          AND ($6::uuid IS NULL OR i.owner_user_id = $6::uuid)
          AND ($7::text IS NULL OR i.status = $7::text)
    ),
    cost AS (
        SELECT s.initiative_id,
               SUM(s.amount) FILTER (WHERE s.currency = ev.currency)::float8 AS total,
               SUM(s.amount) FILTER (WHERE s.currency = ev.currency AND s.status IN {ACTUAL_STATUSES!r})::float8 AS actual,
               {", ".join(f"SUM(s.amount) FILTER (WHERE s.currency = ev.currency AND s.status IN {ACTUAL_STATUSES!r} AND s.cost_category = '{c}')::float8 AS cat_{c.lower()}" for c in COST_CATEGORIES)},
               count(*) FILTER (WHERE s.currency <> ev.currency) AS other_currency_lines
        FROM (
            SELECT s.initiative_id, s.amount, s.currency, s.status, {_category_case()} AS cost_category
            FROM spend_lines s
            LEFT JOIN spend_categories sc ON sc.id = s.category_id
            LEFT JOIN expense_types et ON et.id = sc.expense_type_id
        ) s
        JOIN ev ON ev.id = s.initiative_id
        GROUP BY s.initiative_id
    ),
    -- Where a synced value and a typed-in one exist for the same month and result, the synced one counts
    picked AS (
        SELECT DISTINCT ON (o.initiative_id, o.period_start, o.metric_id) o.initiative_id, o.metric_id, o.value, o.currency
        FROM outcomes o JOIN ev ON ev.id = o.initiative_id
        ORDER BY o.initiative_id, o.period_start, o.metric_id, (o.source = 'MANUAL'), o.recorded_at DESC
    ),
    res AS (
        SELECT p.initiative_id,
               {", ".join(f"SUM(p.value) FILTER (WHERE m.code = '{code}')::float8 AS {name}" for name, code in COUNTS.items())},
               SUM(p.value) FILTER (WHERE m.code = 'REVENUE' AND p.currency = ev.currency)::float8 AS revenue
        FROM picked p
        JOIN outcome_metrics m ON m.id = p.metric_id
        JOIN ev ON ev.id = p.initiative_id
        GROUP BY p.initiative_id
    ),
    -- Events with a demo log count demos from it (only those that have happened), even while none have yet
    demo_log AS (
        SELECT d.initiative_id, count(*) FILTER (WHERE {HELD_DEMO_SQL}) AS held
        FROM event_demos d
        JOIN demo_outcomes o ON o.id = d.outcome_id
        JOIN ev ON ev.id = d.initiative_id
        GROUP BY d.initiative_id
    )
    SELECT ev.*,
           COALESCE(cost.total, 0)::float8 AS total_cost,
           COALESCE(cost.actual, 0)::float8 AS actual_cost,
           {", ".join(f"COALESCE(cost.cat_{c.lower()}, 0)::float8 AS cat_{c.lower()}" for c in COST_CATEGORIES)},
           COALESCE(cost.other_currency_lines, 0)::int AS other_currency_lines,
           {", ".join(f"res.{name}" for name in COUNTS if name != "demos")}, res.revenue,
           CASE WHEN demo_log.initiative_id IS NOT NULL THEN NULLIF(demo_log.held, 0)::float8 ELSE res.demos END AS demos
    FROM ev
    LEFT JOIN cost ON cost.initiative_id = ev.id
    LEFT JOIN res ON res.initiative_id = ev.id
    LEFT JOIN demo_log ON demo_log.initiative_id = ev.id
    ORDER BY ev.start_date DESC, ev.name, ev.id
"""

# For each event (id, start): last year's event with the same name key, and its actual cost in its own currency.
# Any event in the Events channel counts, never the event itself; closest to exactly a year earlier wins.
_PRIOR_YEAR_SQL = f"""
    WITH cur AS (
        SELECT i.id, i.start_date, {name_key_sql('i.name')} AS name_key
        FROM initiatives i WHERE i.id = ANY($1::uuid[])
    )
    SELECT cur.id, py.id AS prior_id, py.currency AS prior_currency,
           (SELECT COALESCE(SUM(s.amount), 0)::float8 FROM spend_lines s
            WHERE s.initiative_id = py.id AND s.currency = py.currency AND s.status IN {ACTUAL_STATUSES!r}) AS prior_cost
    FROM cur
    JOIN LATERAL (
        SELECT i.id, i.currency
        FROM initiatives i
        JOIN channels c ON c.id = i.channel_id AND c.code = '{EVENTS_CHANNEL}'
        WHERE i.id <> cur.id
          AND i.start_date BETWEEN cur.start_date - {PRIOR_YEAR_MAX_DAYS} AND cur.start_date - {PRIOR_YEAR_MIN_DAYS}
          AND {name_key_sql('i.name')} = cur.name_key
        ORDER BY abs(i.start_date - (cur.start_date - interval '1 year')::date), i.start_date DESC, i.id
        LIMIT 1
    ) py ON true
"""


def _year_earlier(day: date) -> date:
    """The same day a year earlier; 29 February becomes 28 February."""
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def _previous_period(date_from: date | None, date_to: date | None) -> tuple[date, date] | None:
    """The same number of days, immediately before the period. None unless both ends are set."""
    if date_from is None or date_to is None:
        return None
    end = date_from - timedelta(days=1)
    return end - (date_to - date_from), end


def _year_window(window: tuple[date, date] | None) -> tuple[date, date] | None:
    return (_year_earlier(window[0]), _year_earlier(window[1])) if window else None


def _sum(values: list[float | None]) -> float | None:
    """Sum of the recorded values; null when none of them was recorded."""
    recorded = [v for v in values if v is not None]
    return round(sum(recorded), 2) if recorded else None


def _per(amount: float | None, count: float | None) -> float | None:
    return round(amount / count, 2) if amount is not None and count else None


def _event(row, prior: dict | None) -> dict:
    actual = round(row["actual_cost"], 2)
    budget = row["budget_amount"]
    revenue = None if row["revenue"] is None else round(row["revenue"], 2)
    # Last year's cost only compares when it's in the same currency (never converted)
    prior_cost = round(prior["prior_cost"], 2) if prior and prior["prior_currency"] == row["currency"] else None
    return {
        "id": row["id"],
        "name": row["name"],
        "event_code": row["event_code"],
        "brand_name": row["brand_name"],
        "market_name": row["market_name"],
        "city": row["city"],
        "country_code": row["country_code"],
        "event_type_name": row["event_type_name"],
        "owner_name": row["owner_name"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "status": row["status"],
        "currency": row["currency"],
        "approved_budget": budget,
        "actual_cost": actual,
        # Estimates and commitments: expected, but no invoice yet
        "awaiting_cost": round(row["total_cost"] - row["actual_cost"], 2),
        "prior_year_cost": prior_cost,
        "prior_year_event_id": prior["prior_id"] if prior else None,
        "variance": None if budget is None else round(actual - budget, 2),
        **{name: row[name] for name in COUNTS},
        "revenue": revenue,
        "cost_per_lead": _per(actual, row["leads"]),
        "roi_percent": metrics.roi_percent(actual, revenue),
        "cost_categories": {c: round(row[f"cat_{c.lower()}"], 2) for c in COST_CATEGORIES},
        "other_currency_lines": row["other_currency_lines"],
    }


def _by_currency(rows) -> dict[str, list]:
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row["currency"], []).append(row)
    return groups


def _figures(rows: list | None) -> dict:
    """Budget, actual spend, variance, leads and cost per lead of one currency's events in a window.
    Everything is null when the window has no events in that currency (or isn't known)."""
    if not rows:
        return {"approved_budget": None, "actual_spend": None, "budget_variance": None, "leads": None, "cost_per_lead": None}
    budgeted = [r for r in rows if r["budget_amount"] is not None]
    budget = round(sum(r["budget_amount"] for r in budgeted), 2) if budgeted else None
    actual = round(sum(r["actual_cost"] for r in rows), 2)
    leads = _sum([r["leads"] for r in rows])
    return {
        "approved_budget": budget,
        "actual_spend": actual,
        # Only the events with a budget are compared with it
        "budget_variance": None if budget is None else round(sum(r["actual_cost"] for r in budgeted) - budget, 2),
        "leads": leads,
        "cost_per_lead": _per(actual, leads),
    }


def _spend(rows: list | None) -> float | None:
    return round(sum(r["actual_cost"] for r in rows), 2) if rows else None


def _month(day: date) -> date:
    return day.replace(day=1)


def _months(first: date, last: date) -> list[date]:
    months, month = [], _month(first)
    while month <= last:
        months.append(month)
        month = (month + timedelta(days=32)).replace(day=1)
    return months


def _monthly(events: list, prior_year: list | None, period: tuple[date | None, date | None]) -> list[dict]:
    """Per currency and month of the period: approved budget, actual spend and last year's spend in that month,
    by the month the event starts (an event that started before the period counts in its first month)."""
    date_from, date_to = period
    starts = [e["start_date"] for e in events]
    if not starts:
        return []
    first = _month(date_from) if date_from else _month(min(starts))
    last = _month(date_to) if date_to else _month(max(starts))
    months = _months(first, last)

    def month_of(start: date, shift_year: bool) -> date:
        month = _month(start)
        if shift_year:
            month = month.replace(year=month.year + 1)
        return max(month, first)

    rows = []
    for currency in sorted({e["currency"] for e in events}):
        figures = {m: {"approved_budget": 0.0, "actual_spend": 0.0, "prior_year_spend": 0.0} for m in months}
        for e in events:
            if e["currency"] == currency and (month := month_of(e["start_date"], False)) in figures:
                figures[month]["approved_budget"] += e["budget_amount"] or 0
                figures[month]["actual_spend"] += e["actual_cost"]
        for e in prior_year or []:
            if e["currency"] == currency and (month := month_of(e["start_date"], True)) in figures:
                figures[month]["prior_year_spend"] += e["actual_cost"]
        rows += [
            {"month": m, "currency": currency, **{k: round(v, 2) for k, v in figures[m].items()}} for m in months
        ]
    return rows


def _funnel(events: list) -> dict:
    return {name: _sum([e[name] for e in events]) for name in COUNTS}


async def list_event_performance(
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    brand_id: int | None = None,
    market_id: int | None = None,
    event_type_id: int | None = None,
    owner_user_id: UUID | None = None,
    status: str | None = None,
) -> dict:
    """Non-draft events in the Events channel whose dates overlap [date_from, date_to], newest first, with totals
    per currency compared with the previous period and a year earlier, monthly rows, the funnel and the cost split."""
    filters = (brand_id, market_id, event_type_id, owner_user_id, status)
    previous = _previous_period(date_from, date_to)
    period_known = date_from is not None and date_to is not None
    prior_year = _year_window((date_from, date_to)) if period_known else None
    previous_prior_year = _year_window(previous)

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_EVENTS_SQL, date_from, date_to, *filters)

        async def window(span: tuple[date, date] | None) -> list | None:
            return await conn.fetch(_EVENTS_SQL, span[0], span[1], *filters) if span else None

        previous_rows = await window(previous)
        prior_year_rows = await window(prior_year)
        previous_prior_year_rows = await window(previous_prior_year)
        priors = {r["id"]: r for r in await conn.fetch(_PRIOR_YEAR_SQL, [r["id"] for r in rows])} if rows else {}

    current = _by_currency(rows)
    by_previous = _by_currency(previous_rows or [])
    by_prior_year = _by_currency(prior_year_rows or [])
    by_previous_prior_year = _by_currency(previous_prior_year_rows or [])

    totals = []
    for currency, group in current.items():
        earlier = by_previous.get(currency) if previous else None
        totals.append({
            "currency": currency,
            "event_count": len(group),
            **_figures(group),
            "prior_year_spend": _spend(by_prior_year.get(currency)) if prior_year else None,
            "previous": {
                **_figures(earlier),
                "prior_year_spend": _spend(by_previous_prior_year.get(currency)) if previous else None,
            },
        })
    totals.sort(key=lambda t: (-t["event_count"], t["currency"]))

    events = [_event(row, priors.get(row["id"])) for row in rows]
    return {
        "period": {"from": date_from, "to": date_to},
        "previous_period": {"from": previous[0], "to": previous[1]} if previous else None,
        "totals": totals,
        "monthly": _monthly(rows, prior_year_rows, (date_from, date_to)),
        "funnel": _funnel(events),
        "cost_breakdown": [
            {"currency": t["currency"],
             **{c: round(sum(e["cost_categories"][c] for e in events if e["currency"] == t["currency"]), 2) for c in COST_CATEGORIES}}
            for t in totals
        ],
        "events": events,
    }
