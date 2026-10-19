"""Event management: every event at a glance and the team going to each one (who, their role, notes).

Travel is booked in Navan; its costs reach events through the Navan cost sync (services/navan_cost_sync_job.py).
"""
from datetime import date
from uuid import UUID

import asyncpg

from models.event_management_model import StaffAssignmentCreate
from postgresql_db.database import get_pool
from services.demo_service import HELD_DEMO_SQL
from services.rbac_service import log_audit
from services.travel_cost_rules import travel_sql

STAFF_AUDIT_ENTITY = "EventStaff"
EVENTS_CHANNEL = "EVENTS"

# Leads first, then the people who run the booth, so the list reads like a team sheet
_ROLE_ORDER = ["LEAD", "BOOTH_STAFF", "DEMO", "SPEAKER", "SETUP", "EXECUTIVE", "OTHER"]


class EventManagementValidationError(Exception):
    """A request that breaks a staffing rule; the message is safe to show to users."""


class InitiativeNotFoundError(Exception):
    """No event with the requested id."""


class StaffAssignmentNotFoundError(Exception):
    """No staff assignment with the requested id."""


# --- Staff assignments ---

_STAFF_SELECT = """
    SELECT s.id, s.initiative_id, s.user_id, u.full_name, u.email, s.role, s.notes, s.updated_at
    FROM event_staff s
    JOIN users u ON u.id = s.user_id
"""

_STAFF_ORDER = f"ORDER BY array_position(ARRAY{_ROLE_ORDER}::text[], s.role), u.full_name NULLS LAST, u.email"

_UPDATABLE_COLUMNS = ("role", "notes")

_CONSTRAINT_MESSAGES = {
    "event_staff_user_id_fkey": "Unknown person.",
    "event_staff_role_check": "Unknown role.",
}


async def _event(conn, initiative_id: UUID):
    row = await conn.fetchrow(
        "SELECT i.id, c.code AS channel FROM initiatives i "
        "JOIN channels c ON c.id = i.channel_id WHERE i.id = $1",
        initiative_id,
    )
    if row is None:
        raise InitiativeNotFoundError
    if row["channel"] != EVENTS_CHANNEL:
        raise EventManagementValidationError("Staff can only be assigned to events.")
    return row


async def list_staff(initiative_id: UUID) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM initiatives WHERE id = $1)", initiative_id):
            raise InitiativeNotFoundError
        rows = await conn.fetch(f"{_STAFF_SELECT} WHERE s.initiative_id = $1 {_STAFF_ORDER}", initiative_id)
    return [dict(r) for r in rows]


async def add_staff(initiative_id: UUID, data: StaffAssignmentCreate, actor_id: UUID) -> dict:
    """Put someone on an event's team and audit it, in one transaction."""
    values = data.model_dump()
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await _event(conn, initiative_id)
                assignment_id = await conn.fetchval(
                    """
                    INSERT INTO event_staff (initiative_id, user_id, role, notes, created_by, updated_by, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $5, clock_timestamp())
                    RETURNING id
                    """,
                    initiative_id, values["user_id"], values["role"], values["notes"], actor_id,
                )
                await log_audit(conn, actor_id, "CREATE", STAFF_AUDIT_ENTITY, assignment_id, new_value={"initiative_id": initiative_id, **values})
        except asyncpg.UniqueViolationError as exc:
            name = await conn.fetchval("SELECT COALESCE(full_name, email) FROM users WHERE id = $1", values["user_id"])
            raise EventManagementValidationError(f"{name or 'This person'} is already on this event.") from exc
        except (asyncpg.ForeignKeyViolationError, asyncpg.CheckViolationError) as exc:
            raise EventManagementValidationError(_CONSTRAINT_MESSAGES.get(getattr(exc, "constraint_name", None), "Invalid details.")) from exc
        row = await conn.fetchrow(f"{_STAFF_SELECT} WHERE s.id = $1", assignment_id)
    return dict(row)


async def update_staff(assignment_id: UUID, changes: dict, actor_id: UUID) -> dict:
    """Apply a partial update and audit what changed (old and new values), in one transaction."""
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                current = await conn.fetchrow(
                    f"SELECT initiative_id, {', '.join(_UPDATABLE_COLUMNS)} FROM event_staff WHERE id = $1 FOR UPDATE",
                    assignment_id,
                )
                if current is None:
                    raise StaffAssignmentNotFoundError
                if "role" in changes and changes["role"] is None:
                    raise EventManagementValidationError("Those details can be changed but not cleared.")
                changed = {k: v for k, v in changes.items() if k in _UPDATABLE_COLUMNS and current[k] != v}
                if changed:
                    columns = [c for c in _UPDATABLE_COLUMNS if c in changed]
                    assignments = ", ".join(f"{c} = ${n}" for n, c in enumerate(columns, start=2))
                    await conn.execute(
                        f"UPDATE event_staff SET {assignments}, updated_at = clock_timestamp(), updated_by = ${len(columns) + 2} "
                        "WHERE id = $1",
                        assignment_id, *[changed[c] for c in columns], actor_id,
                    )
                    initiative = {"initiative_id": current["initiative_id"]}
                    await log_audit(
                        conn, actor_id, "UPDATE", STAFF_AUDIT_ENTITY, assignment_id,
                        old_value={**initiative, **{c: current[c] for c in columns}}, new_value={**initiative, **changed},
                    )
        except asyncpg.CheckViolationError as exc:
            raise EventManagementValidationError(_CONSTRAINT_MESSAGES.get(getattr(exc, "constraint_name", None), "Invalid details.")) from exc
        row = await conn.fetchrow(f"{_STAFF_SELECT} WHERE s.id = $1", assignment_id)
    return dict(row)


async def remove_staff(assignment_id: UUID, actor_id: UUID) -> None:
    """Take someone off an event and audit it."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                f"SELECT initiative_id, user_id, {', '.join(_UPDATABLE_COLUMNS)} FROM event_staff WHERE id = $1 FOR UPDATE",
                assignment_id,
            )
            if current is None:
                raise StaffAssignmentNotFoundError
            await conn.execute("DELETE FROM event_staff WHERE id = $1", assignment_id)
            await log_audit(conn, actor_id, "DELETE", STAFF_AUDIT_ENTITY, assignment_id, old_value=dict(current))


# --- Travellers (from Navan) ---


async def list_travellers(initiative_id: UUID) -> list[dict]:
    """The people on an event's active Navan bookings (kept by the Navan sync), by name."""
    pool = get_pool()
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM initiatives WHERE id = $1)", initiative_id):
            raise InitiativeNotFoundError
        rows = await conn.fetch(
            """
            SELECT name, email, department, user_id, booking_types, bookings, amount::float8 AS amount, currency,
                   first_travel, last_travel
            FROM event_travellers WHERE initiative_id = $1
            ORDER BY lower(name), name, navan_person_uuid
            """,
            initiative_id,
        )
    return [dict(r) for r in rows]


# --- Event cards ---

# The same rule as _timing below, in SQL so filtering and ordering happen before the limit; keep them in step
_TIMING_SQL = """(CASE WHEN i.start_date IS NULL THEN 'UNSCHEDULED' WHEN $2::date < i.start_date THEN 'UPCOMING'
                       WHEN $2::date > COALESCE(i.end_date, i.start_date) THEN 'PAST' ELSE 'NOW' END)"""

_OVERVIEW_SQL = f"""
    SELECT i.id, i.name, ed.event_code, b.name AS brand_name, sc.code AS sub_channel_code, sc.name AS sub_channel_name,
           i.status, i.start_date, i.end_date, ed.venue, ed.city, ed.country_code, ed.platform, u.full_name AS owner_name,
           i.budget_amount::float8 AS budget_amount, i.currency,
           COALESCE(cost.expected, 0)::float8 AS expected_cost, COALESCE(cost.actual, 0)::float8 AS actual_cost,
           COALESCE(cost.lines, 0) AS cost_lines,
           ed.travel_budget_amount::float8 AS travel_budget_amount,
           COALESCE(cost.travel_expected, 0)::float8 AS travel_expected,
           COALESCE(cost.travel_spent, 0)::float8 AS travel_spent,
           ed.navan_estimate_amount::float8 AS navan_estimate_amount, cost.navan_actual::float8 AS navan_actual,
           COALESCE(st.staff_count, 0) AS staff_count, COALESCE(tr.traveller_count, 0) AS traveller_count,
           COALESCE(dm.given, 0) AS demos_given, COALESCE(dm.booked, 0) AS demos_booked,
           res.leads, COALESCE(res.has_results, false) AS has_results
    FROM initiatives i
    JOIN channels c ON c.id = i.channel_id AND c.code = '{EVENTS_CHANNEL}'
    JOIN brands b ON b.id = i.brand_id
    LEFT JOIN sub_channels sc ON sc.id = i.sub_channel_id
    LEFT JOIN event_details ed ON ed.initiative_id = i.id
    LEFT JOIN users u ON u.id = i.owner_user_id
    LEFT JOIN LATERAL (
        SELECT SUM(s.amount) FILTER (WHERE s.currency = i.currency) AS expected,
               SUM(s.amount) FILTER (WHERE s.currency = i.currency AND s.status IN ('INVOICED', 'PAID')) AS actual,
               count(*) AS lines,
               -- Travel (from Navan or entered here), in the event's currency: all of it, and invoiced + paid
               SUM(s.amount) FILTER (WHERE {travel_sql("s")} AND s.currency = i.currency) AS travel_expected,
               SUM(s.amount) FILTER (WHERE {travel_sql("s")} AND s.currency = i.currency
                                     AND s.status IN ('INVOICED', 'PAID')) AS travel_spent,
               -- The event's active Navan bookings (the sync's lines), in the event's currency; null without any
               SUM(s.amount) FILTER (WHERE s.source = 'NAVAN' AND s.currency = i.currency) AS navan_actual
        FROM spend_lines s WHERE s.initiative_id = i.id
    ) cost ON true
    LEFT JOIN LATERAL (
        SELECT count(*) AS staff_count FROM event_staff WHERE initiative_id = i.id
    ) st ON true
    LEFT JOIN LATERAL (
        SELECT count(*) AS traveller_count FROM event_travellers WHERE initiative_id = i.id
    ) tr ON true
    LEFT JOIN LATERAL (
        SELECT count(*) FILTER (WHERE {HELD_DEMO_SQL}) AS given,
               count(*) FILTER (WHERE d.demo_at > now() AND o.code <> 'NO_SHOW') AS booked
        FROM event_demos d JOIN demo_outcomes o ON o.id = d.outcome_id WHERE d.initiative_id = i.id
    ) dm ON true
    LEFT JOIN LATERAL (
        SELECT SUM(o.value) FILTER (WHERE m.code = 'LEADS')::float8 AS leads, count(*) > 0 AS has_results
        FROM outcomes o JOIN outcome_metrics m ON m.id = o.metric_id WHERE o.initiative_id = i.id
    ) res ON true
    WHERE ($1::text IS NULL OR i.name ILIKE $1 OR ed.event_code ILIKE $1 OR ed.city ILIKE $1)
      AND ($3::text IS NULL OR {_TIMING_SQL} = $3)
    ORDER BY CASE {_TIMING_SQL} WHEN 'NOW' THEN 0 WHEN 'UPCOMING' THEN 1 WHEN 'UNSCHEDULED' THEN 2 ELSE 3 END,
             CASE WHEN i.start_date > $2::date THEN i.start_date - $2::date
                  WHEN COALESCE(i.end_date, i.start_date) < $2::date THEN $2::date - COALESCE(i.end_date, i.start_date)
                  ELSE 0 END,
             i.name
    LIMIT 500
"""


def _timing(start: date | None, end: date | None, today: date) -> tuple[str, int | None]:
    if start is None:
        return "UNSCHEDULED", None
    last = end or start
    if today < start:
        return "UPCOMING", (start - today).days
    if today > last:
        return "PAST", (today - last).days
    return "NOW", None


async def list_event_overview(search: str | None = None, timing: str | None = None, today: date | None = None) -> list[dict]:
    """Every event with what still needs doing: on now first, then upcoming (soonest first), then past (most recent first)."""
    pattern = f"%{search.strip()}%" if search and search.strip() else None
    pool = get_pool()
    async with pool.acquire() as conn:
        # The database's date, so "today" doesn't depend on the server's time zone
        today = today or await conn.fetchval("SELECT CURRENT_DATE")
        rows = await conn.fetch(_OVERVIEW_SQL, pattern, today, timing)

    events = []
    for row in rows:
        item = dict(row)
        item["timing"], item["days_away"] = _timing(item["start_date"], item["end_date"], today)
        events.append(item)
    return events
