"""Demos given at events: one row per demo, with its type, company, product, who gave it and the outcome.

The log is the record of demos for an event, so the demo count, cost per demo and how many demos turned
into customers all come from it. Only the organisation is stored; people's details stay in HubSpot.
"""
from datetime import date
from uuid import UUID

import asyncpg

from models.demo_model import DemoCreate
from postgresql_db.database import get_pool
from services.rbac_service import log_audit

AUDIT_ENTITY = "Demo"

# Which logged demos count as demos given: the date has passed and it wasn't a no-show. A demo booked at the
# booth for a later date only counts once it has happened, so bookings can't make an event look cheaper per
# demo. Used here and by Results (services/performance_service.py) so the two always agree.
# Expects event_demos as d and demo_outcomes as o.
HELD_DEMO_SQL = "d.demo_at <= now() AND o.code <> 'NO_SHOW'"
EVENTS_CHANNEL = "EVENTS"

_SELECT = """
    SELECT d.id, d.initiative_id, d.demo_at,
           d.demo_type_id, dt.code AS demo_type_code, dt.name AS demo_type_name,
           d.company, d.hubspot_url,
           d.brand_id, b.name AS brand_name, d.product_note,
           d.given_by, u.full_name AS given_by_name,
           d.outcome_id, o.code AS outcome_code, o.name AS outcome_name, o.funnel_rank AS outcome_rank,
           d.outcome_updated_at, d.note, d.source, d.updated_at
    FROM event_demos d
    JOIN demo_types dt ON dt.id = d.demo_type_id
    JOIN demo_outcomes o ON o.id = d.outcome_id
    LEFT JOIN brands b ON b.id = d.brand_id
    LEFT JOIN users u ON u.id = d.given_by
"""

# Database rule → message a person can act on
_CONSTRAINT_MESSAGES = {
    "event_demos_demo_type_id_fkey": "Unknown demo type.",
    "event_demos_outcome_id_fkey": "Unknown outcome.",
    "event_demos_brand_id_fkey": "Unknown brand.",
    "event_demos_given_by_fkey": "Unknown staff member.",
    "event_demos_hubspot_url_check": "HubSpot link must start with https://",
    "event_demos_source_check": "Unknown source.",
}

# Columns a PATCH may set, in a fixed order (keeps the generated SQL safe from field-name injection)
_UPDATABLE_COLUMNS = (
    "demo_at", "demo_type_id", "company", "hubspot_url", "brand_id", "product_note", "given_by", "outcome_id", "note",
)


class DemoValidationError(Exception):
    """A request that breaks a demo rule; the message is safe to show to users."""


class DemoNotFoundError(Exception):
    """No demo with the requested id."""


class InitiativeNotFoundError(Exception):
    """No initiative with the requested id."""


def _constraint_error(exc: asyncpg.PostgresError) -> DemoValidationError:
    return DemoValidationError(_CONSTRAINT_MESSAGES.get(getattr(exc, "constraint_name", None), "Invalid demo details."))


async def list_demos(initiative_id: UUID) -> list[dict]:
    """An initiative's demos, newest first."""
    pool = get_pool()
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM initiatives WHERE id = $1)", initiative_id):
            raise InitiativeNotFoundError
        rows = await conn.fetch(f"{_SELECT} WHERE d.initiative_id = $1 ORDER BY d.demo_at DESC, d.created_at DESC", initiative_id)
    return [dict(r) for r in rows]


async def create_demo(initiative_id: UUID, data: DemoCreate, actor_id: UUID) -> dict:
    """Log a demo on an event and audit it, in one transaction."""
    values = data.model_dump()
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                initiative = await conn.fetchrow(
                    "SELECT i.brand_id, c.code AS channel FROM initiatives i JOIN channels c ON c.id = i.channel_id WHERE i.id = $1",
                    initiative_id,
                )
                if initiative is None:
                    raise InitiativeNotFoundError
                if initiative["channel"] != EVENTS_CHANNEL:
                    raise DemoValidationError("Demos can only be logged on events.")

                values["brand_id"] = values["brand_id"] or initiative["brand_id"]
                values["given_by"] = values["given_by"] or actor_id
                if values["outcome_id"] is None:
                    values["outcome_id"] = await conn.fetchval("SELECT id FROM demo_outcomes WHERE code = 'PENDING'")

                demo_id = await conn.fetchval(
                    """
                    INSERT INTO event_demos (initiative_id, demo_at, demo_type_id, company, hubspot_url, brand_id,
                                             product_note, given_by, outcome_id, note, source, created_by, updated_by)
                    VALUES ($1, COALESCE($2, now()), $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $12)
                    RETURNING id
                    """,
                    initiative_id, values["demo_at"], values["demo_type_id"], values["company"], values["hubspot_url"],
                    values["brand_id"], values["product_note"], values["given_by"], values["outcome_id"], values["note"],
                    values["source"], actor_id,
                )
                await log_audit(conn, actor_id, "CREATE", AUDIT_ENTITY, demo_id, new_value={"initiative_id": initiative_id, **values})
        except (asyncpg.ForeignKeyViolationError, asyncpg.CheckViolationError) as exc:
            raise _constraint_error(exc) from exc

        row = await conn.fetchrow(f"{_SELECT} WHERE d.id = $1", demo_id)
    return dict(row)


async def update_demo(demo_id: UUID, changes: dict, actor_id: UUID) -> dict:
    """Apply a partial update and audit what changed (old and new values), in one transaction."""
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                current = await conn.fetchrow(
                    f"SELECT initiative_id, {', '.join(_UPDATABLE_COLUMNS)} FROM event_demos WHERE id = $1 FOR UPDATE",
                    demo_id,
                )
                if current is None:
                    raise DemoNotFoundError
                if "demo_type_id" in changes and changes["demo_type_id"] is None:
                    raise DemoValidationError("Choose a demo type.")
                if "outcome_id" in changes and changes["outcome_id"] is None:
                    raise DemoValidationError("Choose an outcome.")

                changed = {k: v for k, v in changes.items() if k in _UPDATABLE_COLUMNS and current[k] != v}
                if changed:
                    columns = [c for c in _UPDATABLE_COLUMNS if c in changed]
                    assignments = ", ".join(f"{c} = ${n}" for n, c in enumerate(columns, start=2))
                    # When the outcome changes, remember when, so "updated after the event" is visible
                    outcome_stamp = ", outcome_updated_at = now()" if "outcome_id" in changed else ""
                    await conn.execute(
                        f"UPDATE event_demos SET {assignments}{outcome_stamp}, updated_at = now(), "
                        f"updated_by = ${len(columns) + 2} WHERE id = $1",
                        demo_id, *[changed[c] for c in columns], actor_id,
                    )
                    initiative = {"initiative_id": current["initiative_id"]}
                    await log_audit(
                        conn, actor_id, "UPDATE", AUDIT_ENTITY, demo_id,
                        old_value={**initiative, **{c: current[c] for c in columns}},
                        new_value={**initiative, **changed},
                    )
        except (asyncpg.ForeignKeyViolationError, asyncpg.CheckViolationError) as exc:
            raise _constraint_error(exc) from exc

        row = await conn.fetchrow(f"{_SELECT} WHERE d.id = $1", demo_id)
    return dict(row)


async def delete_demo(demo_id: UUID, actor_id: UUID) -> None:
    """Delete a demo and audit what it was, in one transaction."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                f"SELECT initiative_id, source, {', '.join(_UPDATABLE_COLUMNS)} FROM event_demos WHERE id = $1 FOR UPDATE",
                demo_id,
            )
            if current is None:
                raise DemoNotFoundError
            await conn.execute("DELETE FROM event_demos WHERE id = $1", demo_id)
            await log_audit(conn, actor_id, "DELETE", AUDIT_ENTITY, demo_id, old_value=dict(current))


# --- Summary ---

# Counts by type, outcome, brand and staff member, plus the overall total, in one grouped pass
_SUMMARY_SQL = f"""
    SELECT GROUPING(dt.id) = 0 AS by_type,
           GROUPING(o.id) = 0 AS by_outcome,
           GROUPING(d.brand_id) = 0 AS by_brand,
           GROUPING(d.given_by) = 0 AS by_given_by,
           dt.code AS type_code, dt.name AS type_name, dt.sort_order AS type_order,
           o.code AS outcome_code, o.name AS outcome_name, o.sort_order AS outcome_order, o.funnel_rank,
           d.brand_id, b.name AS brand_name,
           d.given_by, u.full_name AS given_by_name,
           COUNT(*) AS demos
    FROM event_demos d
    JOIN demo_types dt ON dt.id = d.demo_type_id
    JOIN demo_outcomes o ON o.id = d.outcome_id
    LEFT JOIN brands b ON b.id = d.brand_id
    LEFT JOIN users u ON u.id = d.given_by
    WHERE d.initiative_id = $1 AND {HELD_DEMO_SQL}
    GROUP BY GROUPING SETS (
        (dt.id, dt.code, dt.name, dt.sort_order),
        (o.id, o.code, o.name, o.sort_order, o.funnel_rank),
        (d.brand_id, b.name),
        (d.given_by, u.full_name),
        ()
    )
"""

# Demos booked for later, no-shows, and how many different companies the demos given reached. A demo with no
# company recorded counts as its own company, so leaving the field blank never shrinks the count.
_DEMO_EXTRAS_SQL = f"""
    SELECT count(*) FILTER (WHERE d.demo_at > now() AND o.code <> 'NO_SHOW') AS upcoming,
           count(*) FILTER (WHERE o.code = 'NO_SHOW') AS no_shows,
           count(DISTINCT lower(btrim(d.company))) FILTER (WHERE {HELD_DEMO_SQL} AND nullif(btrim(d.company), '') IS NOT NULL) AS named_companies,
           count(*) FILTER (WHERE {HELD_DEMO_SQL} AND nullif(btrim(d.company), '') IS NULL) AS unnamed_demos
    FROM event_demos d
    JOIN demo_outcomes o ON o.id = d.outcome_id
    WHERE d.initiative_id = $1
"""

# The event's cost in its own currency: expected (estimates included) and actual (invoiced or paid) so far.
# Other currencies are flagged, never converted.
_COST_SQL = """
    SELECT i.currency, i.end_date,
           COALESCE(SUM(s.amount) FILTER (WHERE s.currency = i.currency), 0) AS expected_cost,
           COALESCE(SUM(s.amount) FILTER (WHERE s.currency = i.currency AND s.status IN ('INVOICED', 'PAID')), 0) AS actual_cost,
           COALESCE(bool_or(s.currency <> i.currency), false) AS mixed_currencies
    FROM initiatives i
    LEFT JOIN spend_lines s ON s.initiative_id = i.id
    WHERE i.id = $1
    GROUP BY i.currency, i.end_date
"""


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None


def _per(cost: float, count: int) -> float | None:
    return round(cost / count, 2) if count else None


async def get_demo_summary(initiative_id: UUID) -> dict:
    """How many demos were given, of which kind, by whom, how far they got, and what each one cost.

    Only demos that have happened count (see HELD_DEMO_SQL); upcoming bookings and no-shows are reported
    separately. Customers here are customers won through demos, not every customer the event brought in.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        cost = await conn.fetchrow(_COST_SQL, initiative_id)
        if cost is None:
            raise InitiativeNotFoundError
        rows = await conn.fetch(_SUMMARY_SQL, initiative_id)
        extras = await conn.fetchrow(_DEMO_EXTRAS_SQL, initiative_id)

    total = 0
    by_type, by_outcome, by_brand, by_given_by = [], [], [], []
    funnel = {"with_outcome": 0, "follow_ups": 0, "deals": 0, "customers": 0}

    for row in rows:
        count = row["demos"]
        if row["by_type"]:
            by_type.append((row["type_order"], {"key": row["type_code"], "name": row["type_name"], "count": count}))
        elif row["by_outcome"]:
            by_outcome.append((row["outcome_order"], {"key": row["outcome_code"], "name": row["outcome_name"], "count": count}))
            rank = row["funnel_rank"]
            if row["outcome_code"] != "PENDING":
                funnel["with_outcome"] += count
            if rank >= 1:
                funnel["follow_ups"] += count
            if rank >= 3:
                funnel["deals"] += count
            if row["outcome_code"] == "CUSTOMER":
                funnel["customers"] += count
        elif row["by_brand"]:
            by_brand.append({"key": str(row["brand_id"] or ""), "name": row["brand_name"] or "Not set", "count": count})
        elif row["by_given_by"]:
            by_given_by.append({"key": str(row["given_by"] or ""), "name": row["given_by_name"] or "Not recorded", "count": count})
        else:
            total = count

    expected_cost = round(float(cost["expected_cost"]), 2)
    actual_cost = round(float(cost["actual_cost"]), 2)
    companies = extras["named_companies"] + extras["unnamed_demos"]
    end_date = cost["end_date"]
    return {
        "total": total,
        "upcoming": extras["upcoming"],
        "no_shows": extras["no_shows"],
        "companies_demoed": companies,
        "funnel": {"demos": total, **funnel},
        "follow_up_rate": _rate(funnel["follow_ups"], funnel["with_outcome"]),
        "customer_rate": _rate(funnel["customers"], total),
        "by_type": [item for _, item in sorted(by_type, key=lambda t: t[0])],
        "by_outcome": [item for _, item in sorted(by_outcome, key=lambda t: t[0])],
        "by_brand": sorted(by_brand, key=lambda c: -c["count"]),
        "by_given_by": sorted(by_given_by, key=lambda c: -c["count"]),
        "expected_cost": expected_cost,
        "actual_cost": actual_cost,
        "currency": cost["currency"],
        "event_ended": bool(end_date and end_date < date.today()),
        "cost_per_demo": _per(expected_cost, total),
        "cost_per_company": _per(expected_cost, companies),
        "cost_per_customer": _per(expected_cost, funnel["customers"]),
        "cost_per_demo_actual": _per(actual_cost, total),
        "cost_per_customer_actual": _per(actual_cost, funnel["customers"]),
        "mixed_currencies": cost["mixed_currencies"],
    }
