"""Event management: every event at a glance, the staff going to each one, and the feed the travel system reads.

Who goes and what they need is recorded here. Flights, hotels, Ubers, stipends and cargo are booked in the travel
management system, which reads the events and staff from GET /api/integrations/travel/events (by event code) and
reports back how far booking has got. Nothing here books travel.
"""
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import asyncpg

from models.event_management_model import StaffAssignmentCreate
from postgresql_db.database import get_pool
from services.demo_service import HELD_DEMO_SQL
from services.rbac_service import log_audit

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


# --- What the travel system needs before it can book someone ---
# The same rule in Python (for one person) and SQL (for counts on the event cards); keep them in step.

def travel_missing(row) -> list[str]:
    """Details still missing before the travel system can book this person."""
    if not row["travel_needed"]:
        return []
    missing = []
    if row["arrive_on"] is None:
        missing.append("arrival date")
    if row["depart_on"] is None:
        missing.append("departure date")
    if row["needs_flight"] and not row["departs_from"]:
        missing.append("where they fly from")
    return missing


def travel_readiness(row) -> str:
    if not row["travel_needed"]:
        return "NOT_TRAVELLING"
    if row["travel_status"]:
        return row["travel_status"]
    return "MISSING_DETAILS" if travel_missing(row) else "READY"


_READY_SQL = "(arrive_on IS NOT NULL AND depart_on IS NOT NULL AND (NOT needs_flight OR departs_from IS NOT NULL))"


# --- Staff assignments ---

_STAFF_SELECT = """
    SELECT s.id, s.initiative_id, s.user_id, u.full_name, u.email, s.role, s.travel_needed, s.departs_from,
           s.arrive_on, s.depart_on, s.needs_flight, s.needs_hotel, s.needs_ground_transport, s.needs_stipend,
           s.cargo_note, s.notes, s.travel_status, s.travel_status_at, s.updated_at
    FROM event_staff s
    JOIN users u ON u.id = s.user_id
"""

_STAFF_ORDER = f"ORDER BY array_position(ARRAY{_ROLE_ORDER}::text[], s.role), u.full_name NULLS LAST, u.email"

# Details the travel system books from; changing any of them after booking means it has to look again
_TRAVEL_COLUMNS = (
    "travel_needed", "departs_from", "arrive_on", "depart_on", "needs_flight", "needs_hotel", "needs_ground_transport",
    "needs_stipend", "cargo_note",
)

_UPDATABLE_COLUMNS = (
    "role", "travel_needed", "departs_from", "arrive_on", "depart_on", "needs_flight", "needs_hotel",
    "needs_ground_transport", "needs_stipend", "cargo_note", "notes",
)

_CONSTRAINT_MESSAGES = {
    "event_staff_user_id_fkey": "Unknown person.",
    "event_staff_role_check": "Unknown role.",
    "event_staff_dates_check": "The departure date can't be before the arrival date.",
}


def _with_readiness(row) -> dict:
    item = dict(row)
    item["readiness"] = travel_readiness(item)
    item["missing"] = travel_missing(item)
    return item


async def _event(conn, initiative_id: UUID):
    row = await conn.fetchrow(
        "SELECT i.id, i.start_date, i.end_date, c.code AS channel FROM initiatives i "
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
    return [_with_readiness(r) for r in rows]


async def add_staff(initiative_id: UUID, data: StaffAssignmentCreate, actor_id: UUID) -> dict:
    """Put someone on an event's team and audit it, in one transaction.

    When they need travel and no dates are given, they arrive the day before it starts (to set up) and leave
    on the last day.
    """
    values = data.model_dump()
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                event = await _event(conn, initiative_id)
                if values["travel_needed"]:
                    if values["arrive_on"] is None and event["start_date"]:
                        values["arrive_on"] = event["start_date"] - timedelta(days=1)
                    if values["depart_on"] is None and event["end_date"]:
                        values["depart_on"] = event["end_date"]
                assignment_id = await conn.fetchval(
                    """
                    INSERT INTO event_staff (initiative_id, user_id, role, travel_needed, departs_from, arrive_on, depart_on,
                                             needs_flight, needs_hotel, needs_ground_transport, needs_stipend, cargo_note,
                                             notes, created_by, updated_by, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $14, clock_timestamp())
                    RETURNING id
                    """,
                    initiative_id, values["user_id"], values["role"], values["travel_needed"], values["departs_from"],
                    values["arrive_on"], values["depart_on"], values["needs_flight"], values["needs_hotel"],
                    values["needs_ground_transport"], values["needs_stipend"], values["cargo_note"], values["notes"], actor_id,
                )
                await log_audit(conn, actor_id, "CREATE", STAFF_AUDIT_ENTITY, assignment_id, new_value={"initiative_id": initiative_id, **values})
        except asyncpg.UniqueViolationError as exc:
            name = await conn.fetchval("SELECT COALESCE(full_name, email) FROM users WHERE id = $1", values["user_id"])
            raise EventManagementValidationError(f"{name or 'This person'} is already on this event.") from exc
        except (asyncpg.ForeignKeyViolationError, asyncpg.CheckViolationError) as exc:
            raise EventManagementValidationError(_CONSTRAINT_MESSAGES.get(getattr(exc, "constraint_name", None), "Invalid details.")) from exc
        row = await conn.fetchrow(f"{_STAFF_SELECT} WHERE s.id = $1", assignment_id)
    return _with_readiness(row)


async def update_staff(assignment_id: UUID, changes: dict, actor_id: UUID) -> dict:
    """Apply a partial update and audit what changed (old and new values), in one transaction.

    Changing travel details after the travel system reported BOOKED clears that status, so the person shows as ready
    (or missing details) again and the travel system picks up the new details on its next read.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                current = await conn.fetchrow(
                    f"SELECT initiative_id, travel_status, {', '.join(_UPDATABLE_COLUMNS)} FROM event_staff WHERE id = $1 FOR UPDATE",
                    assignment_id,
                )
                if current is None:
                    raise StaffAssignmentNotFoundError
                for field in ("role", "travel_needed", "needs_flight", "needs_hotel", "needs_ground_transport", "needs_stipend"):
                    if field in changes and changes[field] is None:
                        raise EventManagementValidationError("Those details can be changed but not cleared.")
                changed = {k: v for k, v in changes.items() if k in _UPDATABLE_COLUMNS and current[k] != v}
                if changed:
                    columns = [c for c in _UPDATABLE_COLUMNS if c in changed]
                    rebook = current["travel_status"] == "BOOKED" and any(c in _TRAVEL_COLUMNS for c in columns)
                    assignments = ", ".join(f"{c} = ${n}" for n, c in enumerate(columns, start=2))
                    if rebook:
                        assignments += ", travel_status = NULL, travel_status_at = NULL"
                    # clock_timestamp, not now(): a change committing after a travel-system read must be stamped after it
                    await conn.execute(
                        f"UPDATE event_staff SET {assignments}, updated_at = clock_timestamp(), updated_by = ${len(columns) + 2} "
                        "WHERE id = $1",
                        assignment_id, *[changed[c] for c in columns], actor_id,
                    )
                    old, new = {c: current[c] for c in columns}, dict(changed)
                    if rebook:
                        old["travel_status"], new["travel_status"] = "BOOKED", None
                    initiative = {"initiative_id": current["initiative_id"]}
                    await log_audit(
                        conn, actor_id, "UPDATE", STAFF_AUDIT_ENTITY, assignment_id,
                        old_value={**initiative, **old}, new_value={**initiative, **new},
                    )
        except asyncpg.CheckViolationError as exc:
            raise EventManagementValidationError(_CONSTRAINT_MESSAGES.get(getattr(exc, "constraint_name", None), "Invalid details.")) from exc
        row = await conn.fetchrow(f"{_STAFF_SELECT} WHERE s.id = $1", assignment_id)
    return _with_readiness(row)


async def remove_staff(assignment_id: UUID, actor_id: UUID) -> None:
    """Take someone off an event and audit it. The travel system sees they're gone on its next read."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                f"SELECT initiative_id, user_id, travel_status, {', '.join(_UPDATABLE_COLUMNS)} FROM event_staff WHERE id = $1 FOR UPDATE",
                assignment_id,
            )
            if current is None:
                raise StaffAssignmentNotFoundError
            await conn.execute("DELETE FROM event_staff WHERE id = $1", assignment_id)
            # The row is gone, so mark the event itself as changed; otherwise updated_since never returns it and the
            # travel system never learns to cancel this person's bookings
            await conn.execute(
                "UPDATE event_details SET updated_at = clock_timestamp() WHERE initiative_id = $1", current["initiative_id"]
            )
            await log_audit(conn, actor_id, "DELETE", STAFF_AUDIT_ENTITY, assignment_id, old_value=dict(current))


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
           COALESCE(st.staff_count, 0) AS staff_count, COALESCE(st.staff_ready, 0) AS staff_ready,
           COALESCE(st.staff_missing, 0) AS staff_missing_details, COALESCE(st.staff_booked, 0) AS staff_booked,
           COALESCE(dm.given, 0) AS demos_given, COALESCE(dm.booked, 0) AS demos_booked,
           res.leads, COALESCE(res.has_results, false) AS has_results
    FROM initiatives i
    JOIN channels c ON c.id = i.channel_id AND c.code = '{EVENTS_CHANNEL}'
    JOIN brands b ON b.id = i.brand_id
    LEFT JOIN sub_channels sc ON sc.id = i.sub_channel_id
    LEFT JOIN event_details ed ON ed.initiative_id = i.id
    LEFT JOIN users u ON u.id = i.owner_user_id
    LEFT JOIN LATERAL (
        SELECT SUM(amount) FILTER (WHERE currency = i.currency) AS expected,
               SUM(amount) FILTER (WHERE currency = i.currency AND status IN ('INVOICED', 'PAID')) AS actual,
               count(*) AS lines
        FROM spend_lines WHERE initiative_id = i.id
    ) cost ON true
    LEFT JOIN LATERAL (
        SELECT count(*) AS staff_count,
               count(*) FILTER (WHERE travel_needed AND travel_status IS NULL AND {_READY_SQL}) AS staff_ready,
               count(*) FILTER (WHERE travel_needed AND travel_status IS NULL AND NOT {_READY_SQL}) AS staff_missing,
               count(*) FILTER (WHERE travel_status = 'BOOKED') AS staff_booked
        FROM event_staff WHERE initiative_id = i.id
    ) st ON true
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
        # The database's date: the same "today" as the travel feed's 90-day window
        today = today or await conn.fetchval("SELECT CURRENT_DATE")
        rows = await conn.fetch(_OVERVIEW_SQL, pattern, today, timing)

    events = []
    for row in rows:
        item = dict(row)
        item["timing"], item["days_away"] = _timing(item["start_date"], item["end_date"], today)
        events.append(item)
    return events


# --- Travel system feed ---

_FEED_EVENTS_SQL = f"""
    SELECT i.id AS initiative_id, ed.event_code, i.name, i.status, b.name AS brand, sc.name AS sub_channel,
           i.start_date, i.end_date, ed.venue, ed.city, ed.country_code, ed.platform,
           u.full_name AS owner_name, u.email AS owner_email,
           GREATEST(i.updated_at, ed.updated_at,
                    (SELECT max(updated_at) FROM event_staff s WHERE s.initiative_id = i.id)) AS updated_at
    FROM initiatives i
    JOIN channels c ON c.id = i.channel_id AND c.code = '{EVENTS_CHANNEL}'
    JOIN event_details ed ON ed.initiative_id = i.id
    JOIN brands b ON b.id = i.brand_id
    LEFT JOIN sub_channels sc ON sc.id = i.sub_channel_id
    LEFT JOIN users u ON u.id = i.owner_user_id
    -- Drafts aren't sent until they're finished, so nobody books travel for a half-entered event
    WHERE i.status <> 'DRAFT' AND (i.end_date IS NULL OR i.end_date >= CURRENT_DATE - 90)
"""


async def travel_feed(updated_since: datetime | None = None) -> list[dict]:
    """Events (current, upcoming and up to 90 days past) with the staff who need travel, for the travel system.

    A person taken off an event simply disappears from its staff list; cancelled events stay with their status.
    A time without a timezone is taken as UTC.
    """
    if updated_since is not None and updated_since.tzinfo is None:
        updated_since = updated_since.replace(tzinfo=timezone.utc)
    pool = get_pool()
    async with pool.acquire() as conn:
        events = await conn.fetch(f"{_FEED_EVENTS_SQL} ORDER BY i.start_date NULLS LAST, i.name")
        if updated_since is not None:
            events = [e for e in events if e["updated_at"] and e["updated_at"] > updated_since]
        ids = [e["initiative_id"] for e in events]
        staff = await conn.fetch(
            f"{_STAFF_SELECT} WHERE s.initiative_id = ANY($1::uuid[]) AND s.travel_needed {_STAFF_ORDER}", ids
        ) if ids else []

    by_event: dict[UUID, list[dict]] = {}
    for row in staff:
        item = _with_readiness(row)
        item["assignment_id"] = item.pop("id")
        by_event.setdefault(item["initiative_id"], []).append(item)
    return [{**dict(e), "staff": by_event.get(e["initiative_id"], [])} for e in events]


async def record_travel_status(assignment_id: UUID, status: str) -> None:
    """The travel system reporting how far booking has got (REQUESTED, BOOKED or CANCELLED). Not audited as a person."""
    pool = get_pool()
    async with pool.acquire() as conn:
        updated = await conn.fetchval(
            "UPDATE event_staff SET travel_status = $2, travel_status_at = now() WHERE id = $1 RETURNING id", assignment_id, status
        )
    if updated is None:
        raise StaffAssignmentNotFoundError
