"""Marketing initiatives — one record per initiative, whatever the channel."""
from uuid import UUID

import asyncpg

from models.initiative_model import InitiativeCreate
from postgresql_db.database import get_pool
from services.initiative_metrics import kpi_fits_channel, result_fits_channel
from services.rbac_service import log_audit

AUDIT_ENTITY = "Initiative"

# Columns and joins shared by every query that returns initiatives to the API
_SELECT = """
    SELECT i.id, i.name,
           i.brand_id, b.name AS brand_name,
           i.channel_id, c.name AS channel_name,
           i.sub_channel_id, s.name AS sub_channel_name,
           i.funnel_stage_id, fs.name AS funnel_stage_name,
           i.goal_id, g.name AS goal_name,
           i.primary_kpi_id, k.name AS primary_kpi_name,
           i.owner_user_id, u.full_name AS owner_name,
           i.market_id, mk.name AS market_name,
           i.start_date, i.end_date,
           i.budget_amount::float8 AS budget_amount, i.currency, i.status,
           ed.event_code, ed.event_type_id, et.name AS event_type_name,
           ed.venue, ed.city, ed.country_code, ed.region_id, r.name AS region_name,
           ed.platform, ed.event_url, ed.booth_size_sqm::float8 AS booth_size_sqm,
           ed.travel_budget_amount::float8 AS travel_budget_amount,
           COALESCE(sp.expected_total, 0)::float8 AS expected_total,
           COALESCE(sp.actual_amount, 0)::float8 AS actual_amount,
           COALESCE(sp.has_mixed_currencies, false) AS has_mixed_currencies
    FROM initiatives i
    JOIN brands b ON b.id = i.brand_id
    JOIN channels c ON c.id = i.channel_id
    LEFT JOIN sub_channels s ON s.id = i.sub_channel_id
    LEFT JOIN funnel_stages fs ON fs.id = i.funnel_stage_id
    LEFT JOIN goals g ON g.id = i.goal_id
    LEFT JOIN primary_kpis k ON k.id = i.primary_kpi_id
    LEFT JOIN users u ON u.id = i.owner_user_id
    LEFT JOIN markets mk ON mk.id = i.market_id
    LEFT JOIN event_details ed ON ed.initiative_id = i.id
    LEFT JOIN event_types et ON et.id = ed.event_type_id
    LEFT JOIN regions r ON r.id = ed.region_id
    -- Cost totals in the initiative's own currency; costs in other currencies are flagged, never converted
    LEFT JOIN LATERAL (
        SELECT SUM(amount) FILTER (WHERE currency = i.currency) AS expected_total,
               SUM(amount) FILTER (WHERE currency = i.currency AND status IN ('INVOICED', 'PAID')) AS actual_amount,
               bool_or(currency <> i.currency) AS has_mixed_currencies
        FROM spend_lines
        WHERE initiative_id = i.id
    ) sp ON true
"""

# Database rule → message a person can act on
_CONSTRAINT_MESSAGES = {
    "initiatives_brand_id_fkey": "Unknown brand.",
    "initiatives_channel_id_fkey": "Unknown channel.",
    "initiatives_sub_channel_id_fkey": "Unknown sub-channel.",
    "initiatives_channel_sub_channel_fkey": "That sub-channel doesn't belong to the chosen channel.",
    "initiatives_funnel_stage_id_fkey": "Unknown funnel stage.",
    "initiatives_goal_id_fkey": "Unknown goal.",
    "initiatives_goal_funnel_stage_fkey": "That goal isn't allowed for the chosen funnel stage.",
    "initiatives_goal_requires_stage_check": "Choose a funnel stage before choosing a goal.",
    "initiatives_primary_kpi_id_fkey": "Unknown main KPI.",
    "initiatives_owner_user_id_fkey": "Unknown owner.",
    "initiatives_market_id_fkey": "Unknown market.",
    "initiatives_dates_check": "End date can't be before the start date.",
    "initiatives_budget_check": "Budget can't be negative.",
    "initiatives_status_check": "Unknown status.",
    "event_details_event_type_id_fkey": "Unknown event type.",
    "event_details_region_id_fkey": "Unknown region.",
    "event_details_country_code_check": "Country must be a two-letter code, e.g. US.",
    "event_details_event_url_check": "Event link must start with http:// or https://",
    "event_details_booth_size_check": "Booth size must be more than zero.",
    "event_details_travel_budget_check": "Travel budget can't be negative.",
}

EVENTS_CHANNEL = "EVENTS"

# Event fields each sub-channel requires: (fields, message). Still an open question with Marketing,
# so it lives here in one place. Sub-channels not listed (Virtual Events, Webinars) need nothing.
EVENT_REQUIRED_FIELDS: dict[str, tuple[tuple[str, ...], str]] = {
    "IN_PERSON_EVENTS": (("city", "country_code"), "In-person events need a city and a country."),
    "SPONSORSHIPS": (("country_code",), "Sponsorships need a country."),
}

# In-person sub-channels can't have a platform or event link. Virtual ones may still have a location
# (some webinars are hosted from an office), so location fields are never blocked.
IN_PERSON_SUB_CHANNELS = {"IN_PERSON_EVENTS", "SPONSORSHIPS"}
_VIRTUAL_ONLY_FIELDS = ("platform", "event_url")

_EVENT_FIELDS = (
    "event_type_id", "venue", "city", "country_code", "region_id", "platform", "event_url", "booth_size_sqm",
    "travel_budget_amount",
)
_EVENT_COLUMNS = ", ".join(_EVENT_FIELDS)
# The same columns for reading back, with amounts as floats so they compare equal to what the API received
_NUMERIC_EVENT_FIELDS = {"booth_size_sqm", "travel_budget_amount"}
_EVENT_SELECT_COLUMNS = ", ".join(f"{f}::float8 AS {f}" if f in _NUMERIC_EVENT_FIELDS else f for f in _EVENT_FIELDS)

TRAVEL_BUDGET_TOO_HIGH = "The travel budget has to be smaller than the event budget."
TRAVEL_BUDGET_NEEDS_BUDGET = "Set the event budget before the travel budget."
EVENT_IN_TRAVEL_SYSTEM = "This event may already be in the travel system. Cancel it instead."


def _check_event_rules(sub_channel_code: str | None, event: dict, *, draft: bool = False) -> None:
    """Raise if an event's details (as they'll be saved) break the sub-channel's rules.

    A draft can leave required fields empty; they're checked when it's finished.
    """
    required, message = EVENT_REQUIRED_FIELDS.get(sub_channel_code, ((), ""))
    if not draft and any(event[field] is None for field in required):
        raise InitiativeValidationError(message)
    if sub_channel_code in IN_PERSON_SUB_CHANNELS and any(event[f] is not None for f in _VIRTUAL_ONLY_FIELDS):
        raise InitiativeValidationError("Platform and event link are for virtual events.")
    if event["booth_size_sqm"] is not None and sub_channel_code not in IN_PERSON_SUB_CHANNELS:
        raise InitiativeValidationError("Booth size is for in-person events.")


def _check_travel_budget(budget_amount: float | None, travel_budget_amount: float | None, *, draft: bool = False) -> None:
    """The travel budget is part of the event budget (same currency), so it has to be smaller than it.

    A draft can have a travel budget before the event budget is set; it's checked when the draft is finished.
    """
    if travel_budget_amount is None:
        return
    if budget_amount is None:
        if not draft:
            raise InitiativeValidationError(TRAVEL_BUDGET_NEEDS_BUDGET)
        return
    if travel_budget_amount >= budget_amount:
        raise InitiativeValidationError(TRAVEL_BUDGET_TOO_HIGH)


class InitiativeValidationError(Exception):
    """A request that breaks a business rule; the message is safe to show to users."""


class InitiativeNotFoundError(Exception):
    """No initiative with the requested id."""


# Allowed status changes. Completed and Cancelled can be reopened so a mistake isn't permanent.
# A draft can be finished into any status, but nothing goes back to being a draft.
STATUS_TRANSITIONS: dict[str, set[str]] = {
    "DRAFT": {"PLANNED", "ACTIVE", "COMPLETED", "PAUSED", "CANCELLED"},
    "PLANNED": {"ACTIVE", "CANCELLED"},
    "ACTIVE": {"PAUSED", "COMPLETED", "CANCELLED"},
    "PAUSED": {"ACTIVE", "CANCELLED"},
    "COMPLETED": {"ACTIVE"},
    "CANCELLED": {"PLANNED"},
}

_STATUS_LABELS = {"DRAFT": "Draft", "PLANNED": "Planned", "ACTIVE": "Active", "COMPLETED": "Completed", "PAUSED": "Paused", "CANCELLED": "Cancelled"}

# Columns a PATCH may set, in a fixed order (keeps the generated SQL safe from field-name injection)
_UPDATABLE_COLUMNS = (
    "name", "brand_id", "channel_id", "sub_channel_id", "funnel_stage_id", "goal_id", "primary_kpi_id",
    "owner_user_id", "start_date", "end_date", "budget_amount", "currency", "status", "market_id",
)


def _constraint_error(exc: asyncpg.PostgresError) -> InitiativeValidationError:
    message = _CONSTRAINT_MESSAGES.get(getattr(exc, "constraint_name", None), "Invalid initiative details.")
    return InitiativeValidationError(message)


async def list_initiatives(
    *,
    channel_id: int | None = None,
    sub_channel_id: int | None = None,
    brand_id: int | None = None,
    market_id: int | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return one page of initiatives (newest start date first) and the total match count."""
    conditions: list[str] = []
    args: list = []

    def add(condition: str, value) -> None:
        args.append(value)
        conditions.append(condition.format(n=len(args)))

    if channel_id is not None:
        add("i.channel_id = ${n}", channel_id)
    if sub_channel_id is not None:
        add("i.sub_channel_id = ${n}", sub_channel_id)
    if brand_id is not None:
        add("i.brand_id = ${n}", brand_id)
    if market_id is not None:
        # No market counts as Unassigned, as it does for budgets
        add("COALESCE(i.market_id, (SELECT id FROM markets WHERE code = 'UNASSIGNED')) = ${n}", market_id)
    if status:
        add("i.status = ${n}", status)
    if search and search.strip():
        # Name or event code, so typing "EVT-2026-0007" finds the event
        add("(i.name ILIKE ${n} OR ed.event_code ILIKE ${n})", f"%{search.strip()}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    args.extend([limit, offset])

    sql = f"""
        SELECT page.*, COUNT(*) OVER () AS total_count
        FROM ({_SELECT} {where}) AS page
        ORDER BY page.start_date DESC NULLS LAST, page.name
        LIMIT ${len(args) - 1} OFFSET ${len(args)}
    """

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        if rows:
            total = rows[0]["total_count"]
        else:
            # Page past the end: still report the real total
            total = await conn.fetchval(
                f"SELECT count(*) FROM initiatives i LEFT JOIN event_details ed ON ed.initiative_id = i.id {where}",
                *args[:-2],
            )

    return [{k: v for k, v in dict(r).items() if k != "total_count"} for r in rows], total


async def create_initiative(data: InitiativeCreate, actor_id: UUID) -> dict:
    """Insert an initiative and its audit entry in one transaction; return it as the list shows it."""
    values = data.model_dump()
    values["owner_user_id"] = values["owner_user_id"] or actor_id
    event = values.pop("event")

    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                codes = await conn.fetchrow(
                    "SELECT c.code AS channel, s.code AS sub_channel "
                    "FROM channels c LEFT JOIN sub_channels s ON s.id = $2 WHERE c.id = $1",
                    values["channel_id"], values["sub_channel_id"],
                )
                if codes is None:
                    raise InitiativeValidationError(_CONSTRAINT_MESSAGES["initiatives_channel_id_fkey"])
                await _check_kpi_fits_channel(conn, values["primary_kpi_id"], values["channel_id"])
                is_event = codes["channel"] == EVENTS_CHANNEL
                if event is not None and not is_event:
                    raise InitiativeValidationError("Event details can only be added to initiatives in the Events channel.")
                if is_event:
                    # Every event gets a details row (and so a code), even with no details sent
                    event = event or dict.fromkeys(_EVENT_FIELDS)
                    _check_event_rules(codes["sub_channel"], event, draft=values["status"] == "DRAFT")
                    _check_travel_budget(values["budget_amount"], event["travel_budget_amount"], draft=values["status"] == "DRAFT")

                initiative_id = await conn.fetchval(
                    """
                    INSERT INTO initiatives (name, brand_id, channel_id, sub_channel_id,
                                             funnel_stage_id, goal_id, primary_kpi_id, owner_user_id,
                                             start_date, end_date, budget_amount, currency, status,
                                             market_id, created_by, updated_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $15)
                    RETURNING id
                    """,
                    values["name"], values["brand_id"], values["channel_id"], values["sub_channel_id"],
                    values["funnel_stage_id"], values["goal_id"], values["primary_kpi_id"], values["owner_user_id"],
                    values["start_date"], values["end_date"], values["budget_amount"], values["currency"],
                    values["status"], values["market_id"], actor_id,
                )
                if is_event:
                    event_code = await _insert_event_details(conn, initiative_id, event, actor_id)
                    values["event"] = {**event, "event_code": event_code}
                await log_audit(conn, actor_id, "CREATE", AUDIT_ENTITY, initiative_id, new_value=values)
        except (asyncpg.ForeignKeyViolationError, asyncpg.CheckViolationError) as exc:
            raise _constraint_error(exc) from exc

        row = await conn.fetchrow(f"{_SELECT} WHERE i.id = $1", initiative_id)
    return dict(row)


async def get_initiative(initiative_id: UUID) -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"{_SELECT} WHERE i.id = $1", initiative_id)
    if row is None:
        raise InitiativeNotFoundError
    return dict(row)


async def _insert_event_details(conn, initiative_id: UUID, event: dict, actor_id: UUID) -> str:
    """Create an initiative's event details row; the database generates and returns the event code."""
    placeholders = ", ".join(f"${n}" for n in range(2, len(_EVENT_FIELDS) + 2))
    actor = f"${len(_EVENT_FIELDS) + 2}"
    return await conn.fetchval(
        f"""
        INSERT INTO event_details (initiative_id, {_EVENT_COLUMNS}, created_by, updated_by)
        VALUES ($1, {placeholders}, {actor}, {actor})
        RETURNING event_code
        """,
        initiative_id, *[event[f] for f in _EVENT_FIELDS], actor_id,
    )


async def _check_kpi_fits_channel(conn, kpi_id: int | None, channel_id: int) -> None:
    """A primary KPI has to make sense for the channel, e.g. no CPM (an ad measure) on a trade show."""
    if kpi_id is None:
        return
    row = await conn.fetchrow(
        "SELECT k.code, k.name, c.code AS channel, c.name AS channel_name "
        "FROM primary_kpis k CROSS JOIN channels c WHERE k.id = $1 AND c.id = $2",
        kpi_id, channel_id,
    )
    if row is not None and not kpi_fits_channel(row["code"], row["channel"]):
        raise InitiativeValidationError(f"{row['name']} isn't used to judge {row['channel_name']}. Choose another main KPI.")


async def _check_results_fit_channel(conn, initiative_id: UUID, channel_id: int) -> None:
    """Moving to another channel can't leave results it doesn't record, e.g. ad impressions on an event."""
    rows = await conn.fetch(
        "SELECT DISTINCT m.code, m.name, m.sort_order, c.code AS channel, c.name AS channel_name "
        "FROM outcomes o JOIN outcome_metrics m ON m.id = o.metric_id CROSS JOIN channels c "
        "WHERE o.initiative_id = $1 AND c.id = $2 ORDER BY m.sort_order",
        initiative_id, channel_id,
    )
    misfits = [r["name"] for r in rows if not result_fits_channel(r["code"], r["channel"])]
    if misfits:
        raise InitiativeValidationError(
            f"{rows[0]['channel_name']} doesn't record {', '.join(misfits)}. Remove those results before moving it."
        )


async def _apply_event_changes(
    conn, initiative_id: UUID, actor_id: UUID, *,
    channel_id: int, sub_channel_id: int | None, current_event: dict | None, event_changes: dict, draft: bool,
    budget_amount: float | None,
) -> tuple[dict | None, dict | None, str | None]:
    """Validate and save an initiative's event details as part of an update.

    Rules use the channel, sub-channel and budget after the update, so lowering the event budget to the
    travel budget or below is refused too. Returns (old, new, event_code), where old/new
    hold only the event fields that changed; new is None when nothing changed.
    """
    codes = await conn.fetchrow(
        "SELECT c.code AS channel, s.code AS sub_channel "
        "FROM channels c LEFT JOIN sub_channels s ON s.id = $2 WHERE c.id = $1",
        channel_id, sub_channel_id,
    )
    if codes is None:
        raise InitiativeValidationError(_CONSTRAINT_MESSAGES["initiatives_channel_id_fkey"])

    if codes["channel"] != EVENTS_CHANNEL:
        if current_event is not None:
            # Never delete event details (and their code) as a side effect of a channel change
            raise InitiativeValidationError("Remove this initiative's event details before moving it to another channel.")
        if event_changes:
            raise InitiativeValidationError("Event details can only be added to initiatives in the Events channel.")
        return None, None, None

    before = {f: current_event[f] for f in _EVENT_FIELDS} if current_event else dict.fromkeys(_EVENT_FIELDS)
    after = {**before, **event_changes}
    _check_event_rules(codes["sub_channel"], after, draft=draft)
    _check_travel_budget(budget_amount, after["travel_budget_amount"], draft=draft)

    if current_event is None:
        # Moving into Events: create the details row, which generates the code
        return None, after, await _insert_event_details(conn, initiative_id, after, actor_id)

    fields = [f for f in _EVENT_FIELDS if after[f] != before[f]]
    if not fields:
        return None, None, current_event["event_code"]
    assignments = ", ".join(f"{f} = ${n}" for n, f in enumerate(fields, start=2))
    # clock_timestamp, not now(): a change committing after a travel-system read must be stamped after it
    await conn.execute(
        f"UPDATE event_details SET {assignments}, updated_at = clock_timestamp(), updated_by = ${len(fields) + 2} "
        "WHERE initiative_id = $1",
        initiative_id, *[after[f] for f in fields], actor_id,
    )
    return {f: before[f] for f in fields}, {f: after[f] for f in fields}, current_event["event_code"]


async def update_initiative(initiative_id: UUID, changes: dict, actor_id: UUID) -> dict:
    """Apply a partial update and audit what changed (old and new values), in one transaction."""
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                current = await conn.fetchrow(
                    f"SELECT {', '.join(_UPDATABLE_COLUMNS)} FROM initiatives WHERE id = $1 FOR UPDATE",
                    initiative_id,
                )
                if current is None:
                    raise InitiativeNotFoundError

                current = dict(current)
                if current["budget_amount"] is not None:
                    current["budget_amount"] = float(current["budget_amount"])
                current_event = await conn.fetchrow(
                    f"SELECT event_code, {_EVENT_SELECT_COLUMNS} FROM event_details WHERE initiative_id = $1 FOR UPDATE",
                    initiative_id,
                )
                changed = {k: v for k, v in changes.items() if k in _UPDATABLE_COLUMNS and current[k] != v}

                new_status = changed.get("status")
                if new_status and new_status not in STATUS_TRANSITIONS[current["status"]]:
                    raise InitiativeValidationError(
                        f"An initiative can't go from {_STATUS_LABELS[current['status']]} to {_STATUS_LABELS[new_status]}."
                    )

                # Only checked when the KPI or channel changes, so older initiatives can still be edited
                if "primary_kpi_id" in changed or "channel_id" in changed:
                    await _check_kpi_fits_channel(
                        conn, changed.get("primary_kpi_id", current["primary_kpi_id"]), changed.get("channel_id", current["channel_id"])
                    )
                if "channel_id" in changed:
                    await _check_results_fit_channel(conn, initiative_id, changed["channel_id"])

                # Event rules apply to the state after this change
                event_old, event_new, event_code = await _apply_event_changes(
                    conn, initiative_id, actor_id,
                    channel_id=changed.get("channel_id", current["channel_id"]),
                    sub_channel_id=changed.get("sub_channel_id", current["sub_channel_id"]),
                    current_event=dict(current_event) if current_event else None,
                    event_changes=changes.get("event") or {},
                    draft=changed.get("status", current["status"]) == "DRAFT",
                    budget_amount=changed["budget_amount"] if "budget_amount" in changed else current["budget_amount"],
                )

                if changed:
                    columns = [c for c in _UPDATABLE_COLUMNS if c in changed]
                    assignments = ", ".join(f"{c} = ${n}" for n, c in enumerate(columns, start=2))
                    await conn.execute(
                        f"""
                        UPDATE initiatives
                        SET {assignments}, updated_at = now(), updated_by = ${len(columns) + 2}
                        WHERE id = $1
                        """,
                        initiative_id, *[changed[c] for c in columns], actor_id,
                    )

                # One audit entry per request, covering the initiative and its event details
                if changed or event_new:
                    old_value = {c: current[c] for c in changed}
                    new_value = dict(changed)
                    if event_new:
                        old_value["event"] = event_old
                        new_value["event"] = {**event_new, "event_code": event_code}
                    await log_audit(
                        conn, actor_id, "UPDATE", AUDIT_ENTITY, initiative_id,
                        old_value=old_value, new_value=new_value,
                    )
        except (asyncpg.ForeignKeyViolationError, asyncpg.CheckViolationError) as exc:
            raise _constraint_error(exc) from exc

        row = await conn.fetchrow(f"{_SELECT} WHERE i.id = $1", initiative_id)
    return dict(row)


async def delete_initiative(initiative_id: UUID, actor_id: UUID) -> None:
    """Delete an initiative (its costs, results and event details go with it) and audit what it was.

    An event that has left Draft is in the travel system's feed and may already have bookings, so it can only be
    cancelled. Drafts and initiatives in other channels can be deleted.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT i.name, i.status, i.channel_id, c.code AS channel, ed.event_code "
                "FROM initiatives i JOIN channels c ON c.id = i.channel_id "
                "LEFT JOIN event_details ed ON ed.initiative_id = i.id WHERE i.id = $1 FOR UPDATE OF i",
                initiative_id,
            )
            if current is None:
                raise InitiativeNotFoundError
            if current["channel"] == EVENTS_CHANNEL and current["status"] != "DRAFT":
                raise InitiativeValidationError(EVENT_IN_TRAVEL_SYSTEM)
            await conn.execute("DELETE FROM initiatives WHERE id = $1", initiative_id)
            old_value = {k: current[k] for k in ("name", "status", "channel_id", "event_code")}
            await log_audit(conn, actor_id, "DELETE", AUDIT_ENTITY, initiative_id, old_value=old_value)


async def list_owner_options(search: str | None = None, limit: int = 20) -> list[dict]:
    """Active users who can own an initiative, matched on name or email."""
    args: list = [limit]
    where = "WHERE is_active AND deleted_at IS NULL"
    if search and search.strip():
        args.append(f"%{search.strip()}%")
        where += " AND (full_name ILIKE $2 OR email ILIKE $2)"

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id, full_name, email FROM users {where} ORDER BY full_name NULLS LAST, email LIMIT $1",
            *args,
        )
    return [dict(r) for r in rows]
