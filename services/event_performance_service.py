"""Events Overview: how each event performed, what it cost and what it brought in, plus totals per currency.

Calculated on request, never stored, with the same rules as the Performance page (performance_service):
- Total event cost = Travel + Booth + Materials + Staff & other: every cost line, estimates included, in the
  event's currency. Lines in other currencies are counted in other_currency_lines, never converted.
- Results are summed across months; a synced value (HubSpot…) beats a typed-in one for the same month and result.
- An event with a demo log counts its demos from the log (demos that have happened), not typed-in numbers.
- Revenue and pipeline are in the event's currency.
- Successful = made a profit (ROI > 0). Breaking even is not a success.

Everything is read in one set-based query for all events in the period, never one query per event.
"""
from datetime import date

from postgresql_db.database import get_pool
from services import initiative_metrics as metrics
from services.demo_service import HELD_DEMO_SQL
from services.performance_service import _DEFAULT_MATURITY_DAYS
from services.travel_cost_rules import travel_sql

EVENTS_CHANNEL = "EVENTS"

# Cost groups by expense type code; anything unmapped is Staff & other. Travel is decided by
# services/travel_cost_rules.py instead (travel feeds count whatever their category).
TRAVEL_GROUP = "TRAVEL"
COST_GROUPS: dict[str, tuple[str, ...]] = {
    "BOOTH": ("SPACE_SPONSORSHIP", "BOOTH_EXHIBIT", "SHOW_SERVICES", "SHIPPING_LOGISTICS"),
    "MATERIALS": ("MARKETING_PROMOTION", "REGISTRATION_HOSPITALITY", "MEDIA"),
}
OTHER_GROUP = "STAFF_OTHER"
GROUPS = (TRAVEL_GROUP, *COST_GROUPS, OTHER_GROUP)

# Result counts on the overview and the KPI each "cost per" is worked out with (initiative_metrics.KPI_DEFINITIONS)
COUNTS = {"visitors": "VISITORS", "leads": "LEADS", "demos": "DEMOS", "follow_ups": "FOLLOW_UPS", "customers": "CUSTOMERS"}
COST_PER = {
    "cost_per_visitor": "CP_VISITOR",
    "cost_per_lead": "CPL",
    "cost_per_demo": "CP_DEMO",
    "cost_per_follow_up": "CP_FOLLOW_UP",
    "cost_per_customer": "CAC",
}


def _group_case() -> str:
    whens = " ".join(f"WHEN et.code IN ({', '.join(repr(c) for c in codes)}) THEN '{group}'" for group, codes in COST_GROUPS.items())
    return f"CASE WHEN {travel_sql('s')} THEN '{TRAVEL_GROUP}' {whens} ELSE '{OTHER_GROUP}' END"


# Same timing rule as the event cards (event_management_service._TIMING_SQL); undated events never get here
_TIMING_SQL = """(CASE WHEN $1::date < i.start_date THEN 'UPCOMING'
                       WHEN $1::date > COALESCE(i.end_date, i.start_date) THEN 'PAST' ELSE 'NOW' END)"""

_PERFORMANCE_SQL = f"""
    WITH ev AS (
        SELECT i.id, i.name, ed.event_code, b.name AS brand_name, mk.name AS market_name,
               sc.code AS sub_channel_code, sc.name AS sub_channel_name, ed.city, ed.country_code, ed.platform,
               i.start_date, i.end_date, {_TIMING_SQL} AS timing, i.status, i.currency,
               i.budget_amount::float8 AS budget_amount,
               -- The initiative's own waiting period, else its sub-channel's default (as performance_service._evaluation)
               COALESCE(ie.maturity_days, df.maturity_days, {_DEFAULT_MATURITY_DAYS}) AS maturity_days
        FROM initiatives i
        JOIN channels c ON c.id = i.channel_id AND c.code = '{EVENTS_CHANNEL}'
        JOIN brands b ON b.id = i.brand_id
        -- No market counts as Unassigned, as it does for budgets and the initiatives list
        LEFT JOIN markets mk ON mk.id = COALESCE(i.market_id, (SELECT id FROM markets WHERE code = 'UNASSIGNED'))
        LEFT JOIN sub_channels sc ON sc.id = i.sub_channel_id
        LEFT JOIN event_details ed ON ed.initiative_id = i.id
        LEFT JOIN initiative_evaluations ie ON ie.initiative_id = i.id
        LEFT JOIN evaluation_defaults df ON df.sub_channel_id = i.sub_channel_id
        WHERE i.status <> 'DRAFT'
          AND i.start_date IS NOT NULL
          AND ($2::date IS NULL OR COALESCE(i.end_date, i.start_date) >= $2::date)
          AND ($3::date IS NULL OR i.start_date <= $3::date)
          AND ($4::int IS NULL OR i.brand_id = $4::int)
          AND ($5::int IS NULL OR COALESCE(i.market_id, (SELECT id FROM markets WHERE code = 'UNASSIGNED')) = $5::int)
          AND ($6::int IS NULL OR i.sub_channel_id = $6::int)
          AND ($7::text IS NULL OR {_TIMING_SQL} = $7::text)
    ),
    cost AS (
        SELECT s.initiative_id,
               SUM(s.amount) FILTER (WHERE s.currency = ev.currency)::float8 AS total,
               {", ".join(f"SUM(s.amount) FILTER (WHERE s.currency = ev.currency AND s.cost_group = '{g}')::float8 AS {g.lower()}" for g in GROUPS)},
               count(*) FILTER (WHERE s.currency <> ev.currency) AS other_currency_lines
        FROM (
            SELECT s.initiative_id, s.amount, s.currency, {_group_case()} AS cost_group
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
               SUM(p.value) FILTER (WHERE m.code = 'REVENUE' AND p.currency = ev.currency)::float8 AS revenue,
               SUM(p.value) FILTER (WHERE m.code = 'PIPELINE' AND p.currency = ev.currency)::float8 AS pipeline
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
           COALESCE(cost.total, 0)::float8 AS cost,
           {", ".join(f"COALESCE(cost.{g.lower()}, 0)::float8 AS cost_{g.lower()}" for g in GROUPS)},
           COALESCE(cost.other_currency_lines, 0)::int AS other_currency_lines,
           res.visitors, res.leads, res.follow_ups, res.customers, res.revenue, res.pipeline,
           CASE WHEN demo_log.initiative_id IS NOT NULL THEN NULLIF(demo_log.held, 0)::float8 ELSE res.demos END AS demos
    FROM ev
    LEFT JOIN cost ON cost.initiative_id = ev.id
    LEFT JOIN res ON res.initiative_id = ev.id
    LEFT JOIN demo_log ON demo_log.initiative_id = ev.id
    ORDER BY CASE ev.timing WHEN 'NOW' THEN 0 WHEN 'UPCOMING' THEN 1 ELSE 2 END,
             CASE WHEN ev.timing = 'UPCOMING' THEN ev.start_date - $1::date
                  WHEN ev.timing = 'PAST' THEN $1::date - COALESCE(ev.end_date, ev.start_date)
                  ELSE 0 END,
             ev.name
"""


def _numbers(cost: float, breakdown: dict[str, float], values: dict[str, float | None]) -> dict:
    """Cost, counts, cost per outcome, ROI and money outcome from one set of (possibly summed) numbers."""
    cost = round(cost, 2)
    counts = {code: values[name] for name, code in COUNTS.items()}
    revenue, pipeline = values["revenue"], values["pipeline"]
    roi = metrics.roi_percent(cost, revenue)
    return {
        "cost": cost,
        "cost_breakdown": {g: round(breakdown[g], 2) for g in GROUPS},
        **{name: values[name] for name in COUNTS},
        "revenue": None if revenue is None else round(revenue, 2),
        "pipeline": None if pipeline is None else round(pipeline, 2),
        **{field: metrics.kpi_actual(kpi, cost, counts, revenue, pipeline) for field, kpi in COST_PER.items()},
        "roi_percent": roi,
        "money_outcome": metrics.money_outcome(cost, revenue, roi),
    }


def event_result(timing: str, roi: float | None, revenue: float | None, matures: date | None, today: date) -> str:
    """UPCOMING before it starts; SUCCESSFUL once it made a profit (even before the window ends);
    otherwise TOO_EARLY inside the maturity window, then NOT_SUCCESSFUL (broke even or lost money) or AWAITING_REVENUE.

    Without a cost ROI can't be worked out, so such an event is treated as having no ROI yet.
    """
    if timing == "UPCOMING":
        return "UPCOMING"
    if roi is not None and roi > 0:
        return "SUCCESSFUL"
    if matures is not None and today < matures:
        return "TOO_EARLY"
    if revenue is not None and roi is not None:
        return "NOT_SUCCESSFUL"
    return "AWAITING_REVENUE"


def _sum(values: list[float | None]) -> float | None:
    """Sum of the recorded values; null when none of them was recorded."""
    recorded = [v for v in values if v is not None]
    return sum(recorded) if recorded else None


def _totals(events: list[dict]) -> list[dict]:
    """Per currency, recomputed from the summed numbers. Most events first."""
    by_currency: dict[str, list[dict]] = {}
    for event in events:
        by_currency.setdefault(event["currency"], []).append(event)
    totals = []
    for currency, group in by_currency.items():
        values = {name: _sum([e[name] for e in group]) for name in (*COUNTS, "revenue", "pipeline")}
        results = [e["result"] for e in group]
        totals.append({
            "currency": currency,
            "event_count": len(group),
            **_numbers(
                sum(e["cost"] for e in group),
                {g: sum(e["cost_breakdown"][g] for e in group) for g in GROUPS},
                values,
            ),
            "successful_count": results.count("SUCCESSFUL"),
            "unsuccessful_count": results.count("NOT_SUCCESSFUL"),
            "awaiting_count": results.count("TOO_EARLY") + results.count("AWAITING_REVENUE"),
        })
    return sorted(totals, key=lambda t: (-t["event_count"], t["currency"]))


async def list_event_performance(
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    brand_id: int | None = None,
    market_id: int | None = None,
    sub_channel_id: int | None = None,
    timing: str | None = None,
    today: date | None = None,
) -> dict:
    """Every non-draft event whose dates overlap [date_from, date_to], with its cost, results and verdict.

    On now first, then upcoming (soonest first), then past (most recent first).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        # The database's date, as the event cards use
        today = today or await conn.fetchval("SELECT CURRENT_DATE")
        rows = await conn.fetch(_PERFORMANCE_SQL, today, date_from, date_to, brand_id, market_id, sub_channel_id, timing)

    events = []
    for row in rows:
        numbers = _numbers(row["cost"], {g: row[f"cost_{g.lower()}"] for g in GROUPS}, {k: row[k] for k in (*COUNTS, "revenue", "pipeline")})
        matures = metrics.matures_on(row["start_date"], row["end_date"], row["maturity_days"])
        events.append({
            "id": row["id"],
            "name": row["name"],
            "event_code": row["event_code"],
            "brand_name": row["brand_name"],
            "market_name": row["market_name"],
            "sub_channel_code": row["sub_channel_code"],
            "sub_channel_name": row["sub_channel_name"],
            "city": row["city"],
            "country_code": row["country_code"],
            "platform": row["platform"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "timing": row["timing"],
            "status": row["status"],
            "currency": row["currency"],
            "budget_amount": row["budget_amount"],
            "other_currency_lines": row["other_currency_lines"],
            **numbers,
            "result": event_result(row["timing"], numbers["roi_percent"], numbers["revenue"], matures, today),
            "matures_on": matures,
        })
    return {"events": events, "totals": _totals(events)}

