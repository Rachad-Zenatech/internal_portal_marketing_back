"""Costs against an initiative (spend lines): list, add, edit and delete, with stage, invoice and boundary rules.

Travel, and anything staff pay and claim back, belongs to the travel management system: those costs
arrive by feed and are never typed in or edited here. Invoices are tracked by number only.
"""
from uuid import UUID

import asyncpg

from models.spend_model import SpendLineCreate
from postgresql_db.database import get_pool
from services.rbac_service import log_audit

AUDIT_ENTITY = "SpendLine"

_SELECT = """
    SELECT s.id, s.initiative_id,
           s.category_id, sc.code AS category_code, sc.name AS category_name,
           et.id AS expense_type_id, et.code AS expense_type_code, et.name AS expense_type_name,
           s.description, s.vendor, s.note, s.status,
           s.estimated_amount::float8 AS estimated_amount, s.amount::float8 AS amount,
           CASE WHEN s.status IN ('INVOICED', 'PAID') AND s.estimated_amount IS NOT NULL
                THEN (s.amount - s.estimated_amount)::float8 END AS variance,
           s.currency, s.incurred_on, s.invoice_ref, s.paid_on, s.payment_method, s.cost_type, s.source,
           s.reconciled_month, u.full_name AS created_by_name, s.updated_at
    FROM spend_lines s
    JOIN spend_categories sc ON sc.id = s.category_id
    JOIN expense_types et ON et.id = sc.expense_type_id
    LEFT JOIN users u ON u.id = s.created_by
"""
_ORDER = "ORDER BY et.sort_order, sc.sort_order, s.incurred_on, s.created_at"

# Database rule → message a person can act on
_CONSTRAINT_MESSAGES = {
    "spend_lines_amount_check": "Amount can't be zero. Use a negative amount for a refund or credit.",
    "spend_lines_paid_on_check": "Add the date it was paid.",
    "spend_lines_status_check": "Unknown status.",
    "spend_lines_currency_check": "Currency must be a three-letter code, e.g. USD.",
    "spend_lines_source_check": "Unknown source.",
    "spend_lines_payment_method_check": "Unknown payment method.",
    "spend_lines_cost_type_check": "Cost type must be OPEX or CAPEX.",
    "spend_lines_category_id_fkey": "Unknown category.",
    "spend_lines_source_ref_key": "This cost has already been imported.",
    "spend_lines_reconciled_status_check": "A reconciled cost must be Invoiced or Paid.",
}

# Which categories a channel can use (spend_categories.applies_to); other channels use ALL only
_CHANNEL_GROUPS = {"EVENTS": "EVENTS", "PAID_SEARCH": "ADS", "PAID_SOCIAL": "ADS"}

# Columns a PATCH may set, in a fixed order (keeps the generated SQL safe from field-name injection)
_UPDATABLE_COLUMNS = (
    "category_id", "description", "vendor", "note", "status", "estimated_amount", "amount", "currency",
    "incurred_on", "invoice_ref", "paid_on", "payment_method", "cost_type",
)


# --- Locks: the single place that decides what can change on a line (used by GET, PATCH and DELETE) ---

# Costs from these sources update automatically, so nothing on them can be edited or deleted
_FEED_LOCKS = {"TRAVEL_FEED": "TRAVEL_FEED", "GOOGLE_ADS": "ADS_IMPORT", "MICROSOFT_ADS": "ADS_IMPORT"}
# Once a month is reconciled the figures are fixed; description, vendor, note and invoice number stay editable
_RECONCILED_FIELDS = ("amount", "currency", "incurred_on", "category_id", "estimated_amount", "cost_type")
# A reconciled line may still move from Invoiced to Paid; after that the payment details are fixed too
_RECONCILED_PAID_FIELDS = ("status", "paid_on", "payment_method")

_LOCK_MESSAGES = {
    "TRAVEL_FEED": "This cost came from the travel management system and updates automatically.",
    "ADS_IMPORT": "This cost came from an ads import and updates automatically.",
}
_RECONCILED_STATUS_MESSAGE = "A reconciled cost can only move from Invoiced to Paid."


def lock_state(line) -> tuple[str | None, list[str], bool]:
    """(lock_reason, locked_fields, can_delete) for a line with source, reconciled_month and status."""
    reason = _FEED_LOCKS.get(line["source"])
    if reason:
        return reason, list(_UPDATABLE_COLUMNS), False
    if line["reconciled_month"] is not None:
        locked = set(_RECONCILED_FIELDS)
        if line["status"] == "PAID":
            locked |= set(_RECONCILED_PAID_FIELDS)
        return "RECONCILED", [f for f in _UPDATABLE_COLUMNS if f in locked], False
    return None, [], True


def _lock_message(reason: str, line) -> str:
    if reason == "RECONCILED":
        return f"This cost was reconciled for {line['reconciled_month'].strftime('%B %Y')}. Ask Finance to reopen it."
    return _LOCK_MESSAGES[reason]


def _with_lock_info(row) -> dict:
    """A line as the API returns it, with what's locked and why."""
    line = dict(row)
    reason, locked, can_delete = lock_state(line)
    line.update(
        lock_reason=reason,
        locked_fields=locked,
        can_delete=can_delete,
        editable=len(locked) < len(_UPDATABLE_COLUMNS),
    )
    return line


class SpendValidationError(Exception):
    """A request that breaks a cost rule; the message is safe to show to users.

    `code` names the rule, so callers such as the spreadsheet import can word it for their own context.
    """

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


# --- Rule checks without database access, shared by the API and the cost sheet import ---

def check_category(category, channel_code: str | None) -> None:
    """Raise if a category (manual_entry_allowed, applies_to) can't be used on an initiative in this channel."""
    if not category["manual_entry_allowed"]:
        raise SpendValidationError("Travel and staff expenses come from the travel management system.", code="MANUAL_ENTRY_NOT_ALLOWED")
    if category["applies_to"] != "ALL" and category["applies_to"] != _CHANNEL_GROUPS.get(channel_code):
        raise SpendValidationError("That category isn't used for this channel.", code="CATEGORY_NOT_FOR_CHANNEL")


def check_paid_date(status: str, paid_on) -> None:
    """A paid cost needs the date it was paid."""
    if status == "PAID" and paid_on is None:
        raise SpendValidationError("Add the date it was paid.", code="PAID_NEEDS_DATE")


def duplicate_invoice_message(invoice_ref: str, vendor: str, initiative_name: str | None, event_code: str | None) -> str:
    """Name the initiative that already has this supplier invoice, so people can find it."""
    if initiative_name is None:
        return f"Invoice {invoice_ref} from {vendor} is already recorded."
    where = f"{event_code} · {initiative_name}" if event_code else initiative_name
    return f"Invoice {invoice_ref} from {vendor} is already recorded on {where}."


class SpendLineNotFoundError(Exception):
    """No cost with the requested id."""


class InitiativeNotFoundError(Exception):
    """No initiative with the requested id."""


async def _category_rules(conn, category_id: int, channel_code: str) -> dict:
    """Return the category's settings, or raise if it can't be used on this initiative."""
    category = await conn.fetchrow(
        "SELECT manual_entry_allowed, applies_to, default_cost_type FROM spend_categories WHERE id = $1 AND is_active",
        category_id,
    )
    if category is None:
        raise SpendValidationError("Unknown category.")
    check_category(category, channel_code)
    return dict(category)


async def _duplicate_invoice_error(conn, vendor: str, invoice_ref: str, line_id: UUID | None) -> SpendValidationError:
    """Name the initiative that already has this supplier invoice, so people can find it."""
    other = await conn.fetchrow(
        """
        SELECT s.vendor, i.name, ed.event_code
        FROM spend_lines s
        JOIN initiatives i ON i.id = s.initiative_id
        LEFT JOIN event_details ed ON ed.initiative_id = i.id
        WHERE lower(s.vendor) = lower($1) AND s.invoice_ref = $2 AND s.id IS DISTINCT FROM $3
        LIMIT 1
        """,
        vendor, invoice_ref, line_id,
    )
    if other is None:
        return SpendValidationError(duplicate_invoice_message(invoice_ref, vendor, None, None))
    return SpendValidationError(duplicate_invoice_message(invoice_ref, other["vendor"], other["name"], other["event_code"]))


async def _translate_db_error(conn, exc: asyncpg.PostgresError, vendor: str | None, invoice_ref: str | None, line_id: UUID | None):
    constraint = getattr(exc, "constraint_name", None)
    if constraint == "spend_lines_vendor_invoice_key" and vendor and invoice_ref:
        return await _duplicate_invoice_error(conn, vendor, invoice_ref, line_id)
    return SpendValidationError(_CONSTRAINT_MESSAGES.get(constraint, "Invalid cost details."))


async def list_spend_lines(initiative_id: UUID) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM initiatives WHERE id = $1)", initiative_id):
            raise InitiativeNotFoundError
        rows = await conn.fetch(f"{_SELECT} WHERE s.initiative_id = $1 {_ORDER}", initiative_id)
    return [_with_lock_info(r) for r in rows]


async def create_spend_line(initiative_id: UUID, data: SpendLineCreate, actor_id: UUID) -> dict:
    """Add a cost to an initiative and audit it, in one transaction."""
    values = data.model_dump()
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                initiative = await conn.fetchrow(
                    "SELECT i.currency, c.code AS channel FROM initiatives i JOIN channels c ON c.id = i.channel_id WHERE i.id = $1",
                    initiative_id,
                )
                if initiative is None:
                    raise InitiativeNotFoundError
                category = await _category_rules(conn, values["category_id"], initiative["channel"])

                values["currency"] = values["currency"] or initiative["currency"]
                values["cost_type"] = values["cost_type"] or category["default_cost_type"]
                if values["status"] == "ESTIMATED" and values["estimated_amount"] is None:
                    # Keep the estimate so the variance shows once the invoice arrives
                    values["estimated_amount"] = values["amount"]
                check_paid_date(values["status"], values["paid_on"])

                line_id = await conn.fetchval(
                    """
                    INSERT INTO spend_lines (initiative_id, category_id, description, vendor, note, status,
                                             estimated_amount, amount, currency, incurred_on, invoice_ref, paid_on,
                                             payment_method, cost_type, source, created_by, updated_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $16)
                    RETURNING id
                    """,
                    initiative_id, values["category_id"], values["description"], values["vendor"], values["note"],
                    values["status"], values["estimated_amount"], values["amount"], values["currency"],
                    values["incurred_on"], values["invoice_ref"], values["paid_on"], values["payment_method"],
                    values["cost_type"], values["source"], actor_id,
                )
                await log_audit(
                    conn, actor_id, "CREATE", AUDIT_ENTITY, line_id,
                    new_value={"initiative_id": initiative_id, **values},
                )
        except (asyncpg.CheckViolationError, asyncpg.ForeignKeyViolationError, asyncpg.UniqueViolationError) as exc:
            raise await _translate_db_error(conn, exc, values["vendor"], values["invoice_ref"], None) from exc

        row = await conn.fetchrow(f"{_SELECT} WHERE s.id = $1", line_id)
    return _with_lock_info(row)


async def update_spend_line(line_id: UUID, changes: dict, actor_id: UUID) -> dict:
    """Apply a partial update and audit what changed (old and new values), in one transaction."""
    pool = get_pool()
    async with pool.acquire() as conn:
        current = None
        try:
            async with conn.transaction():
                current = await conn.fetchrow(
                    f"""
                    SELECT s.initiative_id, s.source, s.reconciled_month, c.code AS channel,
                           s.estimated_amount::float8 AS estimated_amount, s.amount::float8 AS amount,
                           {', '.join(f's.{col}' for col in _UPDATABLE_COLUMNS if col not in ('estimated_amount', 'amount'))}
                    FROM spend_lines s
                    JOIN initiatives i ON i.id = s.initiative_id
                    JOIN channels c ON c.id = i.channel_id
                    WHERE s.id = $1
                    FOR UPDATE OF s
                    """,
                    line_id,
                )
                if current is None:
                    raise SpendLineNotFoundError
                reason, locked, _ = lock_state(current)
                if reason in _LOCK_MESSAGES:
                    raise SpendValidationError(_lock_message(reason, current))

                changed = {k: v for k, v in changes.items() if k in _UPDATABLE_COLUMNS and current[k] != v}
                if reason == "RECONCILED" and "status" in changed and (current["status"], changed["status"]) != ("INVOICED", "PAID"):
                    raise SpendValidationError(_RECONCILED_STATUS_MESSAGE)
                if any(f in locked for f in changed):
                    raise SpendValidationError(_lock_message(reason, current))
                if "category_id" in changed:
                    await _category_rules(conn, changed["category_id"], current["channel"])

                after = {**dict(current), **changed}
                check_paid_date(after["status"], after["paid_on"])

                if changed:
                    # estimated_amount is only ever changed when sent, so moving to INVOICED or PAID keeps the estimate
                    columns = [c for c in _UPDATABLE_COLUMNS if c in changed]
                    assignments = ", ".join(f"{c} = ${n}" for n, c in enumerate(columns, start=2))
                    await conn.execute(
                        f"UPDATE spend_lines SET {assignments}, updated_at = now(), updated_by = ${len(columns) + 2} WHERE id = $1",
                        line_id, *[changed[c] for c in columns], actor_id,
                    )
                    initiative = {"initiative_id": current["initiative_id"]}
                    await log_audit(
                        conn, actor_id, "UPDATE", AUDIT_ENTITY, line_id,
                        old_value={**initiative, **{c: current[c] for c in columns}},
                        new_value={**initiative, **changed},
                    )
        except (asyncpg.CheckViolationError, asyncpg.ForeignKeyViolationError, asyncpg.UniqueViolationError) as exc:
            merged = {**dict(current or {}), **changes}
            raise await _translate_db_error(conn, exc, merged.get("vendor"), merged.get("invoice_ref"), line_id) from exc

        row = await conn.fetchrow(f"{_SELECT} WHERE s.id = $1", line_id)
    return _with_lock_info(row)


async def delete_spend_line(line_id: UUID, actor_id: UUID) -> None:
    """Delete a cost and audit what it was, in one transaction."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                """
                SELECT initiative_id, category_id, description, vendor, note, status,
                       estimated_amount::float8 AS estimated_amount, amount::float8 AS amount, currency,
                       incurred_on, invoice_ref, paid_on, payment_method, cost_type, source, external_ref,
                       reconciled_month
                FROM spend_lines WHERE id = $1 FOR UPDATE
                """,
                line_id,
            )
            if current is None:
                raise SpendLineNotFoundError
            reason, _, can_delete = lock_state(current)
            if not can_delete:
                raise SpendValidationError(_lock_message(reason, current))

            await conn.execute("DELETE FROM spend_lines WHERE id = $1", line_id)
            await log_audit(conn, actor_id, "DELETE", AUDIT_ENTITY, line_id, old_value=dict(current))


# --- Cost summary ---

# One pass over an initiative's lines, grouped two ways at once: by currency + category (with its expense
# type) and by currency + month. Every figure is a filtered sum, so a single grouped query covers them all.
_SUMMARY_SQL = """
    SELECT GROUPING(sc.id) = 0 AS by_category,
           s.currency,
           sc.id AS category_id, sc.name AS category_name, sc.sort_order AS category_order,
           et.id AS expense_type_id, et.code AS expense_type_code, et.name AS expense_type_name,
           et.source AS expense_type_source, et.sort_order AS expense_type_order,
           date_trunc('month', s.incurred_on)::date AS month,
           SUM(s.amount) AS expected_total,
           COALESCE(SUM(s.amount) FILTER (WHERE s.status IN ('COMMITTED', 'INVOICED', 'PAID')), 0) AS committed,
           COALESCE(SUM(s.amount) FILTER (WHERE s.status IN ('INVOICED', 'PAID')), 0) AS actual,
           COALESCE(SUM(s.amount) FILTER (WHERE s.status = 'PAID'), 0) AS paid,
           COALESCE(SUM(s.amount) FILTER (WHERE s.status = 'ESTIMATED'), 0) AS estimated,
           COALESCE(SUM(s.amount - s.estimated_amount)
                    FILTER (WHERE s.status IN ('INVOICED', 'PAID') AND s.estimated_amount IS NOT NULL), 0) AS variance,
           COUNT(*) FILTER (WHERE s.status = 'COMMITTED' AND s.incurred_on < CURRENT_DATE) AS awaiting_count,
           COALESCE(SUM(s.amount) FILTER (WHERE s.status = 'COMMITTED' AND s.incurred_on < CURRENT_DATE), 0) AS awaiting_amount,
           COUNT(*) AS line_count
    FROM spend_lines s
    JOIN spend_categories sc ON sc.id = s.category_id
    JOIN expense_types et ON et.id = sc.expense_type_id
    WHERE s.initiative_id = $1
    GROUP BY GROUPING SETS (
        (s.currency, sc.id, sc.name, sc.sort_order, et.id, et.code, et.name, et.source, et.sort_order),
        (s.currency, date_trunc('month', s.incurred_on))
    )
"""

_FIGURES = ("expected_total", "committed", "actual", "paid", "estimated", "variance", "awaiting_count", "awaiting_amount", "line_count")


def _money(value) -> float:
    return round(float(value), 2)


def _percent(part, whole) -> float | None:
    return round(float(part) / float(whole) * 100, 1) if whole else None


def _add_figures(target: dict, row) -> None:
    for key in _FIGURES:
        target[key] = target.get(key, 0) + row[key]


def _shape_figures(figures, keys: tuple[str, ...]) -> dict:
    return {key: _money(figures.get(key, 0)) for key in keys}


async def get_spend_summary(initiative_id: UUID) -> dict:
    """Costs against the budget, by expense type then category, in the initiative's currency.

    Costs in other currencies are reported separately and never converted.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        initiative = await conn.fetchrow(
            """
            SELECT i.budget_amount, i.currency, i.start_date, i.end_date, ed.booth_size_sqm
            FROM initiatives i LEFT JOIN event_details ed ON ed.initiative_id = i.id
            WHERE i.id = $1
            """,
            initiative_id,
        )
        if initiative is None:
            raise InitiativeNotFoundError
        rows = await conn.fetch(_SUMMARY_SQL, initiative_id)

    currency = initiative["currency"]
    budget = initiative["budget_amount"]
    totals: dict = {}
    types: dict[int, dict] = {}
    months: list[dict] = []
    others: dict[str, dict] = {}

    for row in rows:
        if row["currency"] != currency:
            if row["by_category"]:
                _add_figures(others.setdefault(row["currency"], {}), row)
            continue
        if not row["by_category"]:
            months.append({"month": row["month"], **_shape_figures(dict(row), ("expected_total", "committed", "actual"))})
            continue
        _add_figures(totals, row)
        group = types.setdefault(row["expense_type_id"], {
            "expense_type_id": row["expense_type_id"],
            "code": row["expense_type_code"],
            "name": row["expense_type_name"],
            "source": row["expense_type_source"],
            "order": row["expense_type_order"],
            "figures": {},
            "categories": [],
        })
        _add_figures(group["figures"], row)
        group["categories"].append({
            "category_id": row["category_id"],
            "name": row["category_name"],
            "order": row["category_order"],
            **_shape_figures(dict(row), ("expected_total", "committed", "actual", "estimated", "variance")),
            "line_count": row["line_count"],
        })

    expected = totals.get("expected_total", 0)
    committed = totals.get("committed", 0)
    actual = totals.get("actual", 0)

    by_expense_type = [
        {
            "expense_type_id": group["expense_type_id"],
            "code": group["code"],
            "name": group["name"],
            "source": group["source"],
            **_shape_figures(group["figures"], ("expected_total", "committed", "actual", "estimated", "variance")),
            "percent_of_total": _percent(group["figures"]["expected_total"], expected),
            "categories": [
                {k: v for k, v in c.items() if k != "order"}
                for c in sorted(group["categories"], key=lambda c: c["order"])
            ],
        }
        for group in sorted(types.values(), key=lambda g: g["order"])
    ]

    def expected_from(source: str) -> float:
        return _money(sum(t["figures"]["expected_total"] for t in types.values() if t["source"] == source))

    start, end = initiative["start_date"], initiative["end_date"]
    days = (end - start).days + 1 if start and end else None

    return {
        "budget_amount": _money(budget) if budget is not None else None,
        "budget_currency": currency,
        "totals": {
            **_shape_figures(totals, ("expected_total", "committed", "actual", "paid")),
            "still_estimated": _money(totals.get("estimated", 0)),
            "forecast_remaining": _money(budget - expected) if budget is not None else None,
            "remaining_committed": _money(budget - committed) if budget is not None else None,
            "percent_expected": _percent(expected, budget),
            "percent_committed": _percent(committed, budget),
            "percent_actual": _percent(actual, budget),
            "awaiting_invoice": {"count": totals.get("awaiting_count", 0), "amount": _money(totals.get("awaiting_amount", 0))},
        },
        "by_expense_type": by_expense_type,
        "by_source": {"supplier": expected_from("PORTAL"), "travel": expected_from("TRAVEL_SYSTEM")},
        "by_month": sorted(months, key=lambda m: m["month"]),
        "per_day": _money(expected / days) if days and days > 0 else None,
        "per_sqm": _money(expected / initiative["booth_size_sqm"]) if initiative["booth_size_sqm"] else None,
        "other_currencies": [
            {"currency": code, **_shape_figures(figures, ("expected_total", "committed", "actual"))}
            for code, figures in sorted(others.items())
        ],
        "mixed_currencies": bool(others),
    }
