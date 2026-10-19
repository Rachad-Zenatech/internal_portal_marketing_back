"""Marketing initiatives — one record per initiative, whatever the channel."""
import json
from datetime import date, timedelta
from uuid import UUID

import asyncpg

from models.channel_details_model import details_fields, validate_channel_details
from models.initiative_model import KPI_TARGET_NEEDS_KPI, LINKED_NEEDS_LINK, InitiativeCreate
from postgresql_db.database import get_pool
from services.initiative_metrics import result_fits_channel
from services.rbac_service import log_audit

AUDIT_ENTITY = "Initiative"
EVALUATION_AUDIT_ENTITY = "Evaluation"
_DEFAULT_MATURITY_DAYS = 180
# An active initiative that started this long ago should have costs, results or a campaign link by now
NO_TRACKING_AFTER_DAYS = 14

# Columns and joins shared by every query that returns initiatives to the API
_SELECT = f"""
    SELECT i.id, i.name,
           i.brand_id, b.name AS brand_name,
           i.channel_id, c.name AS channel_name,
           i.sub_channel_id, s.name AS sub_channel_name,
           i.funnel_stage_id, fs.name AS funnel_stage_name,
           i.goal_id, g.name AS goal_name,
           i.owner_user_id, u.full_name AS owner_name,
           i.market_id, mk.name AS market_name,
           i.start_date, i.end_date,
           i.budget_amount::float8 AS budget_amount, i.currency, i.status,
           ed.event_code, ed.event_type_id, et.name AS event_type_name,
           ed.venue, ed.city, ed.country_code, ed.region_id, r.name AS region_name,
           ed.platform, ed.event_url, ed.booth_size_sqm::float8 AS booth_size_sqm,
           ed.travel_budget_amount::float8 AS travel_budget_amount, ed.navan_event_name,
           ed.navan_estimate_amount::float8 AS navan_estimate_amount,
           COALESCE(sp.expected_total, 0)::float8 AS expected_total,
           COALESCE(sp.actual_amount, 0)::float8 AS actual_amount,
           COALESCE(sp.has_mixed_currencies, false) AS has_mixed_currencies,
           i.description,
           i.primary_kpi_id, pk.code AS primary_kpi_code, pk.name AS primary_kpi_name, pk.unit AS primary_kpi_unit,
           i.kpi_target::float8 AS kpi_target, i.data_source, i.channel_details,
           COALESCE(lk.links, '[]'::jsonb) AS links,
           COALESCE(sp.actual_amount, 0)::float8 AS actual_spend,
           CASE WHEN i.budget_amount > 0
                THEN round(COALESCE(sp.actual_amount, 0) / i.budget_amount * 100, 1)::float8 END AS progress_percent,
           att.reasons AS attention_reasons,
           cardinality(att.reasons) > 0 AS needs_attention
    FROM initiatives i
    JOIN brands b ON b.id = i.brand_id
    JOIN channels c ON c.id = i.channel_id
    LEFT JOIN sub_channels s ON s.id = i.sub_channel_id
    LEFT JOIN funnel_stages fs ON fs.id = i.funnel_stage_id
    LEFT JOIN goals g ON g.id = i.goal_id
    LEFT JOIN users u ON u.id = i.owner_user_id
    LEFT JOIN markets mk ON mk.id = i.market_id
    LEFT JOIN primary_kpis pk ON pk.id = i.primary_kpi_id
    LEFT JOIN event_details ed ON ed.initiative_id = i.id
    LEFT JOIN event_types et ON et.id = ed.event_type_id
    LEFT JOIN regions r ON r.id = ed.region_id
    -- Cost totals in the initiative's own currency; costs in other currencies are flagged, never converted
    LEFT JOIN LATERAL (
        SELECT SUM(amount) FILTER (WHERE currency = i.currency) AS expected_total,
               SUM(amount) FILTER (WHERE currency = i.currency AND status IN ('INVOICED', 'PAID')) AS actual_amount,
               bool_or(currency <> i.currency) AS has_mixed_currencies,
               count(*) AS line_count
        FROM spend_lines
        WHERE initiative_id = i.id
    ) sp ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object('id', l.id, 'system', l.system, 'external_id', l.external_id,
                                            'account_id', l.account_id, 'created_at', l.created_at)
                         ORDER BY l.created_at, l.system, l.external_id) AS links,
               count(*) AS link_count
        FROM initiative_links l
        WHERE l.initiative_id = i.id
    ) lk ON true
    -- Results recorded in any form: typed-in or synced results, ad results, or the event's demo log
    CROSS JOIN LATERAL (
        SELECT EXISTS (SELECT 1 FROM outcomes o WHERE o.initiative_id = i.id)
               OR EXISTS (SELECT 1 FROM ad_results a WHERE a.initiative_id = i.id)
               OR EXISTS (SELECT 1 FROM event_demos d WHERE d.initiative_id = i.id) AS has_results
    ) res
    CROSS JOIN LATERAL (
        SELECT array_remove(ARRAY[
            CASE WHEN COALESCE(sp.actual_amount, 0) > i.budget_amount THEN 'OVER_BUDGET' END,
            CASE WHEN i.status <> 'DRAFT' AND i.budget_amount IS NULL THEN 'NO_BUDGET' END,
            CASE WHEN i.status IN ('ACTIVE', 'COMPLETED') AND i.start_date <= CURRENT_DATE - {NO_TRACKING_AFTER_DAYS}
                      AND sp.line_count = 0 AND lk.link_count = 0 AND NOT res.has_results THEN 'NO_TRACKING' END,
            CASE WHEN i.status NOT IN ('DRAFT', 'CANCELLED') AND (i.end_date < CURRENT_DATE OR i.status = 'COMPLETED')
                      AND NOT res.has_results THEN 'NO_RESULTS' END,
            CASE WHEN i.status <> 'DRAFT' AND i.start_date IS NULL THEN 'NO_DATES' END
        ], NULL) AS reasons
    ) att
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
    "initiatives_owner_user_id_fkey": "Unknown owner.",
    "initiatives_market_id_fkey": "Unknown market.",
    "initiatives_dates_check": "End date can't be before the start date.",
    "initiatives_budget_check": "Budget can't be negative.",
    "initiatives_status_check": "Unknown status.",
    "initiatives_primary_kpi_id_fkey": "Unknown primary KPI.",
    "initiatives_kpi_target_check": "KPI target can't be negative.",
    "initiatives_kpi_target_requires_kpi_check": KPI_TARGET_NEEDS_KPI,
    "initiatives_description_length_check": "Description can be at most 1000 characters.",
    "initiatives_data_source_check": "Unknown data source.",
    "initiative_links_system_external_id_key": "That campaign is already linked to another initiative.",
    "initiative_links_system_check": "Unknown campaign system.",
    "event_details_event_type_id_fkey": "Unknown event type.",
    "event_details_region_id_fkey": "Unknown region.",
    "event_details_country_code_check": "Country must be a two-letter code, e.g. US.",
    "event_details_event_url_check": "Event link must start with http:// or https://",
    "event_details_booth_size_check": "Booth size must be more than zero.",
    "event_details_travel_budget_check": "Travel budget can't be negative.",
    "event_details_navan_estimate_check": "Navan estimate can't be negative.",
    "event_details_navan_event_key": "Another event is already linked to that Navan event.",
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
    "travel_budget_amount", "navan_event_name", "navan_estimate_amount",
)
_EVENT_COLUMNS = ", ".join(_EVENT_FIELDS)
# The same columns for reading back, with amounts as floats so they compare equal to what the API received
_NUMERIC_EVENT_FIELDS = {"booth_size_sqm", "travel_budget_amount", "navan_estimate_amount"}
_EVENT_SELECT_COLUMNS = ", ".join(f"{f}::float8 AS {f}" if f in _NUMERIC_EVENT_FIELDS else f for f in _EVENT_FIELDS)

TRAVEL_BUDGET_TOO_HIGH = "The travel budget has to be smaller than the event budget."
TRAVEL_BUDGET_NEEDS_BUDGET = "Set the event budget before the travel budget."
BUDGET_REQUIRED = "Allocated budget is required. Enter 0 if there isn't one."
OWNER_REQUIRED = "Owner is required."

# A channel "has sub-channels" (so one must be chosen) when it has any besides the shared Other
SUB_CHANNEL_REQUIRED_SQL = """EXISTS (SELECT 1 FROM channel_sub_channels l JOIN sub_channels ls ON ls.id = l.sub_channel_id
                                      WHERE l.channel_id = c.id AND ls.code <> 'OTHER' AND ls.is_active)"""

# The chosen channel and sub-channel, and whether the channel has sub-channels to choose from
_CHANNEL_SQL = f"""
    SELECT c.code AS channel, c.name AS channel_name, s.code AS sub_channel,
           {SUB_CHANNEL_REQUIRED_SQL} AS has_sub_channels
    FROM channels c LEFT JOIN sub_channels s ON s.id = $2
    WHERE c.id = $1
"""


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


def _check_required(after: dict, channel, touched) -> None:
    """Fields a non-draft initiative needs (a draft only needs a name, brand and channel).

    touched(field) says whether the field is being set by this request, so editing an older initiative that
    is missing something doesn't fail until that field (or the draft) is touched.
    """
    if after["status"] == "DRAFT":
        return
    if touched("sub_channel_id") and after["sub_channel_id"] is None and channel["has_sub_channels"]:
        raise InitiativeValidationError(f"Choose a sub-channel for {channel['channel_name']}.")
    if touched("budget_amount") and after["budget_amount"] is None:
        raise InitiativeValidationError(BUDGET_REQUIRED)
    if touched("owner_user_id") and after["owner_user_id"] is None:
        raise InitiativeValidationError(OWNER_REQUIRED)


def _channel_details(channel, details: object) -> dict:
    try:
        return validate_channel_details(channel["channel"], channel["channel_name"], details)
    except ValueError as exc:
        raise InitiativeValidationError(str(exc)) from exc


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
    "name", "brand_id", "channel_id", "sub_channel_id", "funnel_stage_id", "goal_id",
    "owner_user_id", "start_date", "end_date", "budget_amount", "currency", "status", "market_id",
    "description", "primary_kpi_id", "kpi_target", "data_source", "channel_details",
)
_JSON_COLUMNS = {"channel_details"}


def _constraint_error(exc: asyncpg.PostgresError) -> InitiativeValidationError:
    message = _CONSTRAINT_MESSAGES.get(getattr(exc, "constraint_name", None), "Invalid initiative details.")
    return InitiativeValidationError(message)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _to_item(row) -> dict:
    """An initiative row as the API returns it (JSON parsed, the KPI as one object)."""
    item = dict(row)
    kpi = {k: item.pop(f"primary_kpi_{k}") for k in ("code", "name", "unit")}
    kpi_id = item.pop("primary_kpi_id")
    item["primary_kpi"] = {"id": kpi_id, **kpi} if kpi_id is not None else None
    item["channel_details"] = _json(item["channel_details"]) or {}
    item["links"] = _json(item["links"]) or []
    item["attention_reasons"] = list(item["attention_reasons"] or [])
    item.pop("total_count", None)
    return item


def _filters(
    *,
    channel_id: int | None = None,
    channel_ids: list[int] | None = None,
    sub_channel_id: int | None = None,
    brand_id: int | None = None,
    market_id: int | None = None,
    owner_user_id: UUID | None = None,
    status: str | None = None,
    search: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    needs_attention: bool | None = None,
) -> tuple[str, list]:
    """WHERE clause (on the `page` subquery of _SELECT) and its arguments."""
    conditions: list[str] = []
    args: list = []

    def add(condition: str, value) -> None:
        args.append(value)
        conditions.append(condition.format(n=len(args)))

    if channel_id is not None:
        add("page.channel_id = ${n}", channel_id)
    if channel_ids:
        add("page.channel_id = ANY(${n}::int[])", channel_ids)
    if sub_channel_id is not None:
        add("page.sub_channel_id = ${n}", sub_channel_id)
    if brand_id is not None:
        add("page.brand_id = ${n}", brand_id)
    if market_id is not None:
        # No market counts as Unassigned, as it does for budgets
        add("COALESCE(page.market_id, (SELECT id FROM markets WHERE code = 'UNASSIGNED')) = ${n}", market_id)
    if owner_user_id is not None:
        add("page.owner_user_id = ${n}", owner_user_id)
    if status:
        add("page.status = ${n}", status)
    if search and search.strip():
        # Name, event code (typing "EVT-2026-0007" finds the event), brand, owner or description
        add(
            "(page.name ILIKE ${n} OR page.event_code ILIKE ${n} OR page.brand_name ILIKE ${n} "
            "OR page.owner_name ILIKE ${n} OR page.description ILIKE ${n})",
            f"%{search.strip()}%",
        )
    # Overlaps the period: starts (or, without a start, ends) by its end, and hasn't ended before its start.
    # An initiative without dates isn't in any period.
    if date_to is not None:
        add("COALESCE(page.start_date, page.end_date) <= ${n}", date_to)
    if date_from is not None:
        add("COALESCE(page.end_date, page.start_date) IS NOT NULL AND COALESCE(page.end_date, 'infinity'::date) >= ${n}", date_from)
    if needs_attention is not None:
        conditions.append("page.needs_attention" if needs_attention else "NOT page.needs_attention")

    return (f"WHERE {' AND '.join(conditions)}" if conditions else ""), args


async def list_initiatives(*, limit: int = 50, offset: int = 0, **filters) -> tuple[list[dict], int]:
    """Return one page of initiatives (newest start date first) and the total match count.

    Filters are those of _filters, e.g. channel_ids lists several channels at once (the sidebar's "Other channels").
    """
    where, args = _filters(**filters)
    args.extend([limit, offset])

    sql = f"""
        SELECT page.*, COUNT(*) OVER () AS total_count
        FROM ({_SELECT}) AS page
        {where}
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
            total = await conn.fetchval(f"SELECT count(*) FROM ({_SELECT}) AS page {where}", *args[:-2])

    return [_to_item(r) for r in rows], total


async def _counts(conn, filters: dict) -> dict:
    where, args = _filters(**filters)
    counts = await conn.fetchrow(
        f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE page.status = 'ACTIVE') AS active,
               count(*) FILTER (WHERE page.needs_attention) AS needs_attention
        FROM ({_SELECT}) AS page {where}
        """,
        *args,
    )
    budgets = await conn.fetch(
        f"""
        SELECT page.currency, SUM(page.budget_amount)::float8 AS amount
        FROM ({_SELECT}) AS page {where}{' AND' if where else ' WHERE'} page.budget_amount IS NOT NULL
        GROUP BY page.currency
        ORDER BY amount DESC, page.currency
        """,
        *args,
    )
    return {**dict(counts), "allocated_budget": [dict(b) for b in budgets]}


async def initiative_summary(**filters) -> dict:
    """Counts and allocated budget for the list's filters, and for the period just before from/to when both are set.

    Needs attention is always judged as of today, for the previous period too.
    """
    filters.pop("needs_attention", None)
    pool = get_pool()
    async with pool.acquire() as conn:
        summary = await _counts(conn, filters)
        summary["previous"] = None
        date_from, date_to = filters.get("date_from"), filters.get("date_to")
        if date_from is not None and date_to is not None:
            days = (date_to - date_from).days + 1
            previous_to = date_from - timedelta(days=1)
            summary["previous"] = await _counts(
                conn, {**filters, "date_from": previous_to - timedelta(days=days - 1), "date_to": previous_to}
            )
    return summary


async def _check_links_free(conn, initiative_id: UUID | None, links: list[dict]) -> None:
    """One external campaign belongs to one initiative."""
    if not links:
        return
    taken = await conn.fetchrow(
        """
        SELECT i.name
        FROM initiative_links l
        JOIN initiatives i ON i.id = l.initiative_id
        JOIN unnest($2::text[], $3::text[]) AS w(system, external_id)
          ON w.system = l.system AND w.external_id = l.external_id
        WHERE l.initiative_id IS DISTINCT FROM $1::uuid
        LIMIT 1
        """,
        initiative_id, [link["system"] for link in links], [link["external_id"] for link in links],
    )
    if taken:
        raise InitiativeValidationError(f"That campaign is already linked to {taken['name']}.")


async def _write_links(conn, initiative_id: UUID, links: list[dict], actor_id: UUID) -> None:
    """Make the initiative's links exactly `links`: remove the others, add new ones, update account ids."""
    systems = [link["system"] for link in links]
    external_ids = [link["external_id"] for link in links]
    await conn.execute(
        """
        DELETE FROM initiative_links l
        WHERE l.initiative_id = $1
          AND NOT EXISTS (SELECT 1 FROM unnest($2::text[], $3::text[]) AS w(system, external_id)
                          WHERE w.system = l.system AND w.external_id = l.external_id)
        """,
        initiative_id, systems, external_ids,
    )
    if links:
        await conn.execute(
            """
            INSERT INTO initiative_links (initiative_id, system, external_id, account_id, created_by)
            SELECT $1, w.system, w.external_id, w.account_id, $5
            FROM unnest($2::text[], $3::text[], $4::text[]) AS w(system, external_id, account_id)
            ON CONFLICT (system, external_id) DO UPDATE SET account_id = EXCLUDED.account_id
            WHERE initiative_links.initiative_id = EXCLUDED.initiative_id
            """,
            initiative_id, systems, external_ids, [link["account_id"] for link in links], actor_id,
        )


async def _current_links(conn, initiative_id: UUID) -> list[dict]:
    rows = await conn.fetch(
        "SELECT system, external_id, account_id FROM initiative_links WHERE initiative_id = $1 "
        "ORDER BY created_at, system, external_id FOR UPDATE",
        initiative_id,
    )
    return [dict(r) for r in rows]


def _link_key(link: dict) -> tuple:
    return link["system"], link["external_id"], link["account_id"]


async def _save_maturity(conn, initiative_id: UUID, sub_channel_id: int | None, maturity_days: int, actor_id: UUID) -> None:
    """Set the initiative's own maturity window and audit it as the evaluation settings page does."""
    row = await conn.fetchrow(
        """
        SELECT e.maturity_days AS own, d.maturity_days AS fallback
        FROM (SELECT 1) one
        LEFT JOIN initiative_evaluations e ON e.initiative_id = $1
        LEFT JOIN evaluation_defaults d ON d.sub_channel_id = $2
        """,
        initiative_id, sub_channel_id,
    )
    if row["own"] == maturity_days:
        return
    before = row["own"] if row["own"] is not None else (row["fallback"] if row["fallback"] is not None else _DEFAULT_MATURITY_DAYS)
    await conn.execute(
        """
        INSERT INTO initiative_evaluations (initiative_id, maturity_days, updated_by, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (initiative_id) DO UPDATE
        SET maturity_days = EXCLUDED.maturity_days, updated_by = EXCLUDED.updated_by, updated_at = now()
        """,
        initiative_id, maturity_days, actor_id,
    )
    await log_audit(
        conn, actor_id, "UPDATE", EVALUATION_AUDIT_ENTITY, initiative_id,
        old_value={"maturity_days": before}, new_value={"maturity_days": maturity_days},
    )


async def create_initiative(data: InitiativeCreate, actor_id: UUID) -> dict:
    """Insert an initiative (with its event details, links and maturity window) and audit it, in one transaction;
    return it as the list shows it."""
    values = data.model_dump()
    values["owner_user_id"] = values["owner_user_id"] or actor_id
    event = values.pop("event")
    links = values.pop("links")
    evaluation = values.pop("evaluation")
    draft = values["status"] == "DRAFT"

    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                channel = await conn.fetchrow(_CHANNEL_SQL, values["channel_id"], values["sub_channel_id"])
                if channel is None:
                    raise InitiativeValidationError(_CONSTRAINT_MESSAGES["initiatives_channel_id_fkey"])
                values["channel_details"] = _channel_details(channel, values["channel_details"])
                is_event = channel["channel"] == EVENTS_CHANNEL
                if event is not None and not is_event:
                    raise InitiativeValidationError("Event details can only be added to initiatives in the Events channel.")
                if is_event:
                    # Every event gets a details row (and so a code), even with no details sent
                    event = event or dict.fromkeys(_EVENT_FIELDS)
                    _check_event_rules(channel["sub_channel"], event, draft=draft)
                    _check_travel_budget(values["budget_amount"], event["travel_budget_amount"], draft=draft)
                _check_required(values, channel, touched=lambda _field: True)
                await _check_links_free(conn, None, links)

                initiative_id = await conn.fetchval(
                    """
                    INSERT INTO initiatives (name, brand_id, channel_id, sub_channel_id,
                                             funnel_stage_id, goal_id, owner_user_id,
                                             start_date, end_date, budget_amount, currency, status,
                                             market_id, description, primary_kpi_id, kpi_target, data_source,
                                             channel_details, created_by, updated_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                            $18::jsonb, $19, $19)
                    RETURNING id
                    """,
                    values["name"], values["brand_id"], values["channel_id"], values["sub_channel_id"],
                    values["funnel_stage_id"], values["goal_id"], values["owner_user_id"],
                    values["start_date"], values["end_date"], values["budget_amount"], values["currency"],
                    values["status"], values["market_id"], values["description"], values["primary_kpi_id"],
                    values["kpi_target"], values["data_source"], json.dumps(values["channel_details"]), actor_id,
                )
                if is_event:
                    event_code = await _insert_event_details(conn, initiative_id, event, actor_id)
                    values["event"] = {**event, "event_code": event_code}
                if links:
                    await _write_links(conn, initiative_id, links, actor_id)
                    values["links"] = links
                await log_audit(conn, actor_id, "CREATE", AUDIT_ENTITY, initiative_id, new_value=values)
                if evaluation and evaluation["maturity_days"] is not None:
                    await _save_maturity(conn, initiative_id, values["sub_channel_id"], evaluation["maturity_days"], actor_id)
        except (asyncpg.ForeignKeyViolationError, asyncpg.CheckViolationError) as exc:
            raise _constraint_error(exc) from exc
        except asyncpg.UniqueViolationError as exc:
            # Only the ones people can fix, e.g. a Navan event already linked to another event
            if getattr(exc, "constraint_name", None) not in _CONSTRAINT_MESSAGES:
                raise
            raise _constraint_error(exc) from exc

        row = await conn.fetchrow(f"{_SELECT} WHERE i.id = $1", initiative_id)
    return _to_item(row)


async def get_initiative(initiative_id: UUID) -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"{_SELECT} WHERE i.id = $1", initiative_id)
    if row is None:
        raise InitiativeNotFoundError
    return _to_item(row)


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
    channel, current_event: dict | None, event_changes: dict, draft: bool, budget_amount: float | None,
) -> tuple[dict | None, dict | None, str | None]:
    """Validate and save an initiative's event details as part of an update.

    Rules use the channel, sub-channel and budget after the update, so lowering the event budget to the
    travel budget or below is refused too. Returns (old, new, event_code), where old/new
    hold only the event fields that changed; new is None when nothing changed.
    """
    if channel["channel"] != EVENTS_CHANNEL:
        if current_event is not None:
            # Never delete event details (and their code) as a side effect of a channel change
            raise InitiativeValidationError("Remove this initiative's event details before moving it to another channel.")
        if event_changes:
            raise InitiativeValidationError("Event details can only be added to initiatives in the Events channel.")
        return None, None, None

    before = {f: current_event[f] for f in _EVENT_FIELDS} if current_event else dict.fromkeys(_EVENT_FIELDS)
    after = {**before, **event_changes}
    _check_event_rules(channel["sub_channel"], after, draft=draft)
    _check_travel_budget(budget_amount, after["travel_budget_amount"], draft=draft)

    if current_event is None:
        # Moving into Events: create the details row, which generates the code
        return None, after, await _insert_event_details(conn, initiative_id, after, actor_id)

    fields = [f for f in _EVENT_FIELDS if after[f] != before[f]]
    if not fields:
        return None, None, current_event["event_code"]
    assignments = ", ".join(f"{f} = ${n}" for n, f in enumerate(fields, start=2))
    # clock_timestamp, not now(): stamped when the change is made, not when the transaction started
    await conn.execute(
        f"UPDATE event_details SET {assignments}, updated_at = clock_timestamp(), updated_by = ${len(fields) + 2} "
        "WHERE initiative_id = $1",
        initiative_id, *[after[f] for f in fields], actor_id,
    )
    return {f: before[f] for f in fields}, {f: after[f] for f in fields}, current_event["event_code"]


async def update_initiative(initiative_id: UUID, changes: dict, actor_id: UUID) -> dict:
    """Apply a partial update and audit what changed (old and new values), in one transaction."""
    changes = dict(changes)
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
                for column in ("budget_amount", "kpi_target"):
                    if current[column] is not None:
                        current[column] = float(current[column])
                current["channel_details"] = _json(current["channel_details"]) or {}
                current_event = await conn.fetchrow(
                    f"SELECT event_code, {_EVENT_SELECT_COLUMNS} FROM event_details WHERE initiative_id = $1 FOR UPDATE",
                    initiative_id,
                )
                after_channel_id = changes.get("channel_id", current["channel_id"])
                after_sub_channel_id = changes["sub_channel_id"] if "sub_channel_id" in changes else current["sub_channel_id"]
                channel = await conn.fetchrow(_CHANNEL_SQL, after_channel_id, after_sub_channel_id)
                if channel is None:
                    raise InitiativeValidationError(_CONSTRAINT_MESSAGES["initiatives_channel_id_fkey"])

                # Channel details belong to the channel: when it changes without new details, keep what still fits
                if "channel_details" in changes:
                    changes["channel_details"] = _channel_details(channel, changes["channel_details"])
                elif after_channel_id != current["channel_id"]:
                    fields = details_fields(channel["channel"])
                    try:
                        changes["channel_details"] = _channel_details(
                            channel, {k: v for k, v in current["channel_details"].items() if k in fields}
                        )
                    except InitiativeValidationError:
                        changes["channel_details"] = {}

                # A target needs a KPI; clearing the KPI clears its target
                after_kpi = changes["primary_kpi_id"] if "primary_kpi_id" in changes else current["primary_kpi_id"]
                if after_kpi is None:
                    if changes.get("kpi_target") is not None:
                        raise InitiativeValidationError(KPI_TARGET_NEEDS_KPI)
                    if current["kpi_target"] is not None:
                        changes["kpi_target"] = None

                changed = {k: v for k, v in changes.items() if k in _UPDATABLE_COLUMNS and current[k] != v}
                after = {**current, **changed}

                new_status = changed.get("status")
                if new_status and new_status not in STATUS_TRANSITIONS[current["status"]]:
                    raise InitiativeValidationError(
                        f"An initiative can't go from {_STATUS_LABELS[current['status']]} to {_STATUS_LABELS[new_status]}."
                    )
                if "channel_id" in changed:
                    await _check_results_fit_channel(conn, initiative_id, changed["channel_id"])

                # Links: the whole set is replaced when sent
                old_links = await _current_links(conn, initiative_id)
                new_links = changes.get("links")
                if new_links is not None:
                    # A partial dump leaves out account ids that weren't sent
                    new_links = [{**link, "account_id": link.get("account_id")} for link in new_links]
                links_changed = new_links is not None and sorted(map(_link_key, old_links)) != sorted(map(_link_key, new_links))
                if after["data_source"] == "LINKED" and not (new_links if new_links is not None else old_links):
                    raise InitiativeValidationError(LINKED_NEEDS_LINK)
                if links_changed:
                    await _check_links_free(conn, initiative_id, new_links)

                # Event rules apply to the state after this change
                event_old, event_new, event_code = await _apply_event_changes(
                    conn, initiative_id, actor_id,
                    channel=channel,
                    current_event=dict(current_event) if current_event else None,
                    event_changes=changes.get("event") or {},
                    draft=after["status"] == "DRAFT",
                    budget_amount=after["budget_amount"],
                )
                leaving_draft = current["status"] == "DRAFT" and after["status"] != "DRAFT"
                _check_required(
                    after, channel,
                    touched=lambda field: leaving_draft or field in changes
                    or (field == "sub_channel_id" and "channel_id" in changed),
                )

                if changed:
                    columns = [c for c in _UPDATABLE_COLUMNS if c in changed]
                    assignments = ", ".join(
                        f"{c} = ${n}::jsonb" if c in _JSON_COLUMNS else f"{c} = ${n}" for n, c in enumerate(columns, start=2)
                    )
                    await conn.execute(
                        f"""
                        UPDATE initiatives
                        SET {assignments}, updated_at = now(), updated_by = ${len(columns) + 2}
                        WHERE id = $1
                        """,
                        initiative_id,
                        *[json.dumps(changed[c]) if c in _JSON_COLUMNS else changed[c] for c in columns],
                        actor_id,
                    )
                if links_changed:
                    await _write_links(conn, initiative_id, new_links, actor_id)

                # One audit entry per request, covering the initiative, its event details and its links
                if changed or event_new or links_changed:
                    old_value = {c: current[c] for c in changed}
                    new_value = dict(changed)
                    if event_new:
                        old_value["event"] = event_old
                        new_value["event"] = {**event_new, "event_code": event_code}
                    if links_changed:
                        old_value["links"] = old_links
                        new_value["links"] = new_links
                    await log_audit(
                        conn, actor_id, "UPDATE", AUDIT_ENTITY, initiative_id,
                        old_value=old_value, new_value=new_value,
                    )
                evaluation = changes.get("evaluation")
                if evaluation and evaluation.get("maturity_days") is not None:
                    await _save_maturity(conn, initiative_id, after["sub_channel_id"], evaluation["maturity_days"], actor_id)
        except (asyncpg.ForeignKeyViolationError, asyncpg.CheckViolationError) as exc:
            raise _constraint_error(exc) from exc
        except asyncpg.UniqueViolationError as exc:
            # Only the ones people can fix, e.g. a Navan event already linked to another event
            if getattr(exc, "constraint_name", None) not in _CONSTRAINT_MESSAGES:
                raise
            raise _constraint_error(exc) from exc

        row = await conn.fetchrow(f"{_SELECT} WHERE i.id = $1", initiative_id)
    return _to_item(row)


async def delete_initiative(initiative_id: UUID, actor_id: UUID) -> None:
    """Delete an initiative in any status and channel (its costs, results, event details, links and team go with
    it) and audit what it was."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT i.name, i.status, i.channel_id, ed.event_code "
                "FROM initiatives i LEFT JOIN event_details ed ON ed.initiative_id = i.id WHERE i.id = $1 FOR UPDATE OF i",
                initiative_id,
            )
            if current is None:
                raise InitiativeNotFoundError
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
