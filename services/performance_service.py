"""Results (outcomes) per month, success settings, scale-or-cut decisions, and the calculated performance view.

How the numbers are put together (calculated on request, never stored):
- Cost is the expected total — estimates included — in the initiative's currency. Costs in other currencies
  are reported, never converted.
- Results are summed across months. Where a synced tool (HubSpot, an ad platform…) and a typed-in value exist for
  the same month and result, the synced one counts, so nothing is counted twice.
- An event that has a demo log counts its demos from the log; they can't also be typed in.
- Revenue is closed revenue in the initiative's currency.
"""
from datetime import date
from uuid import UUID

import asyncpg

from postgresql_db.database import get_pool
from services import initiative_metrics as metrics
from services.rbac_service import log_audit

OUTCOMES_AUDIT_ENTITY = "Outcomes"
EVALUATION_AUDIT_ENTITY = "Evaluation"
DECISION_AUDIT_ENTITY = "Decision"
DEMO_LOG_SOURCE = "DEMO_LOG"
_DEFAULT_MATURITY_DAYS = 180


class PerformanceValidationError(Exception):
    """A request that breaks a results or evaluation rule; the message is safe to show to users."""


class InitiativeNotFoundError(Exception):
    """No initiative with the requested id."""


_INITIATIVE_SQL = """
    SELECT i.id, i.currency, i.start_date, i.end_date, i.sub_channel_id, i.primary_kpi_id,
           EXISTS (SELECT 1 FROM event_demos d WHERE d.initiative_id = i.id) AS has_demo_log
    FROM initiatives i WHERE i.id = $1
"""


async def _initiative(conn, initiative_id: UUID):
    row = await conn.fetchrow(_INITIATIVE_SQL, initiative_id)
    if row is None:
        raise InitiativeNotFoundError
    return row


# --- Results ---

_OUTCOMES_SQL = """
    SELECT o.period_start, m.code AS metric_code, o.value::float8 AS value, o.currency, o.source,
           o.recorded_at, u.full_name AS recorded_by_name
    FROM outcomes o
    JOIN outcome_metrics m ON m.id = o.metric_id
    LEFT JOIN users u ON u.id = o.recorded_by
    WHERE o.initiative_id = $1 AND NOT ($2 AND m.code = 'DEMOS')
    ORDER BY o.period_start, m.sort_order, o.source
"""

# Demos per month from the demo log (months in UTC, like every other date here)
_DEMO_MONTHS_SQL = """
    SELECT date_trunc('month', demo_at AT TIME ZONE 'UTC')::date AS period_start, count(*)::float8 AS value
    FROM event_demos WHERE initiative_id = $1 GROUP BY 1 ORDER BY 1
"""


async def _list_outcomes(conn, initiative) -> dict:
    from_log = initiative["has_demo_log"]
    values = [dict(r) for r in await conn.fetch(_OUTCOMES_SQL, initiative["id"], from_log)]
    if from_log:
        values += [
            {"period_start": r["period_start"], "metric_code": "DEMOS", "value": r["value"], "currency": None,
             "source": DEMO_LOG_SOURCE, "recorded_at": None, "recorded_by_name": None}
            for r in await conn.fetch(_DEMO_MONTHS_SQL, initiative["id"])
        ]
    return {
        "periods": sorted({v["period_start"] for v in values}),
        "values": sorted(values, key=lambda v: v["period_start"]),
        "demos_from_log": from_log,
    }


async def list_outcomes(initiative_id: UUID) -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await _list_outcomes(conn, await _initiative(conn, initiative_id))


async def save_outcomes(initiative_id: UUID, period_start: date, values: list[dict], actor_id: UUID) -> dict:
    """Save the typed-in results for one month: set the values sent, remove those sent as null. Synced values are untouched."""
    if period_start.day != 1:
        raise PerformanceValidationError("Results are recorded per month — use the first day of the month.")
    codes = [v["metric_code"] for v in values]
    if len(codes) != len(set(codes)):
        raise PerformanceValidationError("Each result can only be sent once per month.")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            initiative = await _initiative(conn, initiative_id)
            known = {r["code"]: r for r in await conn.fetch("SELECT id, code, unit FROM outcome_metrics WHERE code = ANY($1::text[])", codes)}
            unknown = [c for c in codes if c not in known]
            if unknown:
                raise PerformanceValidationError(f"Unknown result: {', '.join(unknown)}.")
            if initiative["has_demo_log"] and "DEMOS" in codes:
                raise PerformanceValidationError("Demos are counted from the demo log on this event.")

            current = {
                r["code"]: r["value"]
                for r in await conn.fetch(
                    """
                    SELECT m.code, o.value::float8 AS value FROM outcomes o JOIN outcome_metrics m ON m.id = o.metric_id
                    WHERE o.initiative_id = $1 AND o.period_start = $2 AND o.source = 'MANUAL'
                    """,
                    initiative_id, period_start,
                )
            }
            to_set = [v for v in values if v["value"] is not None and current.get(v["metric_code"]) != v["value"]]
            to_remove = [v["metric_code"] for v in values if v["value"] is None and v["metric_code"] in current]

            if to_set:
                await conn.execute(
                    """
                    INSERT INTO outcomes (initiative_id, period_start, metric_id, value, currency, source, recorded_by, recorded_at)
                    SELECT $1, $2, v.metric_id, v.value, v.currency, 'MANUAL', $6, now()
                    FROM unnest($3::int[], $4::numeric[], $5::text[]) AS v(metric_id, value, currency)
                    ON CONFLICT (initiative_id, period_start, metric_id, source)
                    DO UPDATE SET value = EXCLUDED.value, currency = EXCLUDED.currency,
                                  recorded_by = EXCLUDED.recorded_by, recorded_at = now()
                    """,
                    initiative_id, period_start,
                    [known[v["metric_code"]]["id"] for v in to_set],
                    [v["value"] for v in to_set],
                    # Money results carry a currency (the initiative's unless another is given); counts don't
                    [(v["currency"] or initiative["currency"]) if known[v["metric_code"]]["unit"] == "MONEY" else None for v in to_set],
                    actor_id,
                )
            if to_remove:
                await conn.execute(
                    "DELETE FROM outcomes WHERE initiative_id = $1 AND period_start = $2 AND source = 'MANUAL' AND metric_id = ANY($3::int[])",
                    initiative_id, period_start, [known[c]["id"] for c in to_remove],
                )
            if to_set or to_remove:
                changed = [v["metric_code"] for v in to_set] + to_remove
                await log_audit(
                    conn, actor_id, "UPDATE", OUTCOMES_AUDIT_ENTITY, initiative_id,
                    old_value={"period_start": period_start, **{c: current.get(c) for c in changed}},
                    new_value={"period_start": period_start, **{v["metric_code"]: v["value"] for v in to_set}, **{c: None for c in to_remove}},
                )
        return await _list_outcomes(conn, initiative)


# --- Evaluation settings ---

_KPI_SQL = "SELECT id, code, name FROM primary_kpis WHERE id = $1"


def _kpi_ref(row) -> dict | None:
    if row is None:
        return None
    definition = metrics.KPI_DEFINITIONS.get(row["code"])
    return {
        "id": row["id"], "code": row["code"], "name": row["name"],
        "direction": definition.direction if definition else None,
        "unit": definition.unit if definition else None,
        "supported": definition is not None,
    }


async def _evaluation(conn, initiative) -> dict:
    """The initiative's own settings, filled in from its sub-channel's defaults where it has none."""
    own = await conn.fetchrow(
        """
        SELECT e.target_value::float8 AS target_value, e.maturity_days, e.revenue_basis, e.updated_at, u.full_name AS updated_by_name
        FROM initiative_evaluations e LEFT JOIN users u ON u.id = e.updated_by WHERE e.initiative_id = $1
        """,
        initiative["id"],
    )
    default = None
    if initiative["sub_channel_id"] is not None:
        default = await conn.fetchrow(
            "SELECT primary_kpi_id, maturity_days FROM evaluation_defaults WHERE sub_channel_id = $1", initiative["sub_channel_id"]
        )
    kpi_id = initiative["primary_kpi_id"] or (default["primary_kpi_id"] if default else None)
    kpi = await conn.fetchrow(_KPI_SQL, kpi_id) if kpi_id else None
    return {
        "primary_kpi": _kpi_ref(kpi),
        "kpi_from_default": initiative["primary_kpi_id"] is None and kpi is not None,
        "target_value": own["target_value"] if own else None,
        "maturity_days": own["maturity_days"] if own else (default["maturity_days"] if default else _DEFAULT_MATURITY_DAYS),
        "revenue_basis": own["revenue_basis"] if own else "CLOSED_REVENUE",
        "is_default": own is None,
        "updated_at": own["updated_at"] if own else None,
        "updated_by_name": own["updated_by_name"] if own else None,
    }


async def get_evaluation(initiative_id: UUID) -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await _evaluation(conn, await _initiative(conn, initiative_id))


async def save_evaluation(initiative_id: UUID, target_value: float | None, maturity_days: int, actor_id: UUID) -> dict:
    """Set the target and maturity window, and audit what changed, in one transaction."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            initiative = await _initiative(conn, initiative_id)
            before = await _evaluation(conn, initiative)
            await conn.execute(
                """
                INSERT INTO initiative_evaluations (initiative_id, target_value, maturity_days, updated_by, updated_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (initiative_id) DO UPDATE
                SET target_value = EXCLUDED.target_value, maturity_days = EXCLUDED.maturity_days,
                    updated_by = EXCLUDED.updated_by, updated_at = now()
                """,
                initiative_id, target_value, maturity_days, actor_id,
            )
            if (before["target_value"], before["maturity_days"], before["is_default"]) != (target_value, maturity_days, False):
                await log_audit(
                    conn, actor_id, "UPDATE", EVALUATION_AUDIT_ENTITY, initiative_id,
                    old_value={"target_value": before["target_value"], "maturity_days": before["maturity_days"]},
                    new_value={"target_value": target_value, "maturity_days": maturity_days},
                )
            return await _evaluation(conn, initiative)


# --- Decisions ---

_DECISIONS_SQL = """
    SELECT d.id, d.scale_or_cut_id, s.code, s.name, s.description, d.suggested_rating, d.note,
           u.full_name AS decided_by_name, d.decided_at
    FROM initiative_decisions d
    JOIN scale_or_cut_options s ON s.id = d.scale_or_cut_id
    LEFT JOIN users u ON u.id = d.decided_by
    WHERE d.initiative_id = $1
    ORDER BY d.decided_at DESC
"""


async def list_decisions(initiative_id: UUID) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        await _initiative(conn, initiative_id)
        return [dict(r) for r in await conn.fetch(_DECISIONS_SQL, initiative_id)]


async def record_decision(initiative_id: UUID, scale_or_cut_id: int, note: str | None, actor_id: UUID) -> dict:
    """Record Marketing's decision, with the rating the system suggested at the time, and audit it."""
    suggested = (await get_performance(initiative_id))["rating"]["code"]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                decision_id = await conn.fetchval(
                    """
                    INSERT INTO initiative_decisions (initiative_id, scale_or_cut_id, suggested_rating, note, decided_by)
                    VALUES ($1, $2, $3, $4, $5) RETURNING id
                    """,
                    initiative_id, scale_or_cut_id, suggested, note, actor_id,
                )
            except asyncpg.ForeignKeyViolationError as exc:
                raise PerformanceValidationError("Unknown decision.") from exc
            await log_audit(
                conn, actor_id, "CREATE", DECISION_AUDIT_ENTITY, decision_id,
                new_value={"initiative_id": initiative_id, "scale_or_cut_id": scale_or_cut_id, "suggested_rating": suggested, "note": note},
            )
        row = await conn.fetchrow(f"{_DECISIONS_SQL.replace('WHERE d.initiative_id = $1', 'WHERE d.id = $1')}", decision_id)
    return dict(row)


# --- Performance ---

# Results summed across months; where a synced value and a typed-in one exist for the same month and result,
# the synced one counts (DISTINCT ON keeps the first row: non-manual before manual, newest first)
_TOTALS_SQL = """
    SELECT m.code, m.unit, picked.currency, SUM(picked.value)::float8 AS value
    FROM (
        SELECT DISTINCT ON (o.period_start, o.metric_id) o.metric_id, o.value, o.currency
        FROM outcomes o
        WHERE o.initiative_id = $1
        ORDER BY o.period_start, o.metric_id, (o.source = 'MANUAL'), o.recorded_at DESC
    ) picked
    JOIN outcome_metrics m ON m.id = picked.metric_id
    GROUP BY m.code, m.unit, picked.currency
"""

_COSTS_SQL = "SELECT currency, SUM(amount)::float8 AS amount FROM spend_lines WHERE initiative_id = $1 GROUP BY currency"


async def get_performance(initiative_id: UUID, today: date | None = None) -> dict:
    """Cost per outcome, ROI, the suggested rating and the latest decision for one initiative."""
    today = today or date.today()
    pool = get_pool()
    async with pool.acquire() as conn:
        initiative = await _initiative(conn, initiative_id)
        currency = initiative["currency"]
        cost_rows = await conn.fetch(_COSTS_SQL, initiative_id)
        total_rows = await conn.fetch(_TOTALS_SQL, initiative_id)
        demo_count = await conn.fetchval("SELECT count(*) FROM event_demos WHERE initiative_id = $1", initiative_id)
        evaluation = await _evaluation(conn, initiative)
        latest = await conn.fetchrow(f"{_DECISIONS_SQL} LIMIT 1", initiative_id)

    cost = round(sum(r["amount"] for r in cost_rows if r["currency"] == currency), 2)
    costs_excluded = [{"currency": r["currency"], "amount": round(r["amount"], 2)} for r in cost_rows if r["currency"] != currency]

    totals: dict[str, float] = {}
    money: dict[str, float] = {}
    revenue_excluded: list[dict] = []
    for row in total_rows:
        if row["unit"] == "MONEY":
            if row["currency"] == currency:
                money[row["code"]] = money.get(row["code"], 0) + row["value"]
            else:
                revenue_excluded.append({"currency": row["currency"], "amount": round(row["value"], 2)})
        else:
            totals[row["code"]] = totals.get(row["code"], 0) + row["value"]
    if demo_count:
        # The demo log is the record of demos for this event
        totals["DEMOS"] = float(demo_count)

    revenue = money.get("REVENUE")
    pipeline = money.get("PIPELINE")
    ratios = metrics.summary_ratios(cost, totals, revenue)
    kpi = evaluation["primary_kpi"]
    actual = metrics.kpi_actual(kpi["code"] if kpi else None, cost, totals, revenue, pipeline)
    matures = metrics.matures_on(initiative["start_date"], initiative["end_date"], evaluation["maturity_days"])
    rating = metrics.suggest_rating(
        kpi_code=kpi["code"] if kpi else None,
        kpi_name=kpi["name"] if kpi else None,
        actual=actual,
        target=evaluation["target_value"],
        matures=matures,
        maturity_days=evaluation["maturity_days"],
        today=today,
        roi=ratios["roi_percent"],
        revenue_recorded=bool(revenue),
        currency=currency,
    )

    return {
        "currency": currency,
        "cost": cost,
        "costs_excluded": costs_excluded,
        "totals": {**totals, **money},
        "revenue": revenue,
        "pipeline": pipeline,
        "revenue_excluded": revenue_excluded,
        "ladder": metrics.cost_ladder(cost, totals),
        **ratios,
        "money_outcome": metrics.money_outcome(cost, revenue, ratios["roi_percent"]),
        "has_results": bool(totals or money),
        "demos_from_log": bool(demo_count),
        "evaluation": evaluation,
        "kpi_actual": actual,
        "matures_on": matures,
        "rating": rating,
        "suggested_decision": metrics.SUGGESTED_DECISION[rating["code"]],
        "latest_decision": dict(latest) if latest else None,
    }
