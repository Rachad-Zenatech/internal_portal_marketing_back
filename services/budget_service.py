"""Monthly budgets: what Marketing approved per month, brand, market and channel, and what's been spent against it.

Which budget a cost counts against is worked out every time it's read, never stored. For a cost in a month,
on an initiative with a brand, channel and market (no market counts as Unassigned), the candidates are the
month's budgets for that market that aren't cancelled, whose brand is the initiative's or TBD/All and whose
channel is the initiative's or all channels. The most specific wins: a brand before TBD/All, then a channel
before all channels. Costs no budget covers show as spend without a budget.

Nothing moves money automatically: carryover and borrowing are recorded as money moves (budget_transfers).
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import asyncpg

from postgresql_db.database import get_pool
from services.rbac_service import log_audit
from services.travel_cost_rules import travel_sql

BUDGET_ENTITY = "MonthlyBudget"
TRANSFER_ENTITY = "BudgetTransfer"
COMMENT_ENTITY = "BudgetComment"

UNASSIGNED_MARKET = "UNASSIGNED"
# A brand in the brand list that means "no particular brand"; a budget for it is stored as TBD/All (no brand)
TBD_ALL_BRAND = "TBD_ALL"

# Costs that count as spent: the invoice is in
SPENT_STATUSES = ("INVOICED", "PAID")

# Costs kept out of marketing budgets: travel (services/travel_cost_rules.py). s = the spend line.
_OUTSIDE_BUDGET = travel_sql("s")


class BudgetValidationError(Exception):
    """A request that breaks a budget rule; the message is safe to show to users."""


class BudgetNotFoundError(Exception):
    """No budget (or money move, or comment) with the requested id."""


class BudgetPermissionError(Exception):
    """The person can't do this, e.g. delete someone else's comment."""


# Database rule → message a person can act on
_CONSTRAINT_MESSAGES = {
    "monthly_budgets_brand_id_fkey": "Unknown brand.",
    "monthly_budgets_market_id_fkey": "Unknown market.",
    "monthly_budgets_channel_id_fkey": "Unknown channel.",
    "monthly_budgets_month_check": "Use the first day of the month.",
    "monthly_budgets_amount_check": "Amount can't be negative.",
    "monthly_budgets_currency_check": "Currency must be a three-letter code, e.g. USD.",
    "monthly_budgets_status_check": "Unknown status.",
    "budget_transfers_amount_check": "Amount must be more than zero.",
    "budget_transfers_different_budgets_check": "Choose two different budgets.",
}

_KEY_COLUMNS = ("month", "brand_id", "market_id", "channel_id")
# Columns a PATCH may set, in a fixed order (keeps the generated SQL safe from field-name injection)
_UPDATABLE_COLUMNS = ("brand_id", "market_id", "channel_id", "amount", "currency", "status")


# --- Which budget each cost counts against ($1 = the month) ---

_LINES_CTE = f"""
    lines AS (
        SELECT s.initiative_id, s.amount, s.currency, s.status, i.brand_id, i.channel_id, mk.market_id,
               ob.outside_budget,
               (SELECT b.id FROM monthly_budgets b
                WHERE b.month = $1 AND b.market_id = mk.market_id AND b.status <> 'CANCELLED'
                  AND (b.brand_id = i.brand_id OR b.brand_id IS NULL)
                  AND (b.channel_id = i.channel_id OR b.channel_id IS NULL)
                  AND NOT ob.outside_budget
                -- The most specific budget: a brand before TBD/All, then a channel before all channels
                ORDER BY b.brand_id IS NULL, b.channel_id IS NULL
                LIMIT 1) AS budget_id
        FROM spend_lines s
        JOIN initiatives i ON i.id = s.initiative_id
        CROSS JOIN LATERAL (
            SELECT COALESCE(i.market_id, (SELECT id FROM markets WHERE code = '{UNASSIGNED_MARKET}')) AS market_id
        ) mk
        CROSS JOIN LATERAL (SELECT {_OUTSIDE_BUDGET} AS outside_budget) ob
        WHERE s.incurred_on >= $1 AND s.incurred_on < ($1::date + interval '1 month')
    )
"""

_SPENT = f"COALESCE(SUM(l.amount) FILTER (WHERE l.status IN {SPENT_STATUSES!r}), 0) AS spent"
_EXPECTED = "COALESCE(SUM(l.amount), 0) AS expected"

_BUDGET_FIGURES_SQL = f"""
    WITH {_LINES_CTE}
    SELECT l.budget_id, l.currency, {_SPENT}, {_EXPECTED}
    FROM lines l
    WHERE l.budget_id IS NOT NULL
    GROUP BY l.budget_id, l.currency
"""

_UNBUDGETED_SQL = f"""
    WITH {_LINES_CTE}
    SELECT l.brand_id, br.name AS brand_name, l.market_id, mk.name AS market_name,
           l.channel_id, ch.name AS channel_name, l.currency, {_SPENT}, {_EXPECTED},
           COUNT(DISTINCT l.initiative_id) AS initiative_count
    FROM lines l
    JOIN brands br ON br.id = l.brand_id
    JOIN markets mk ON mk.id = l.market_id
    JOIN channels ch ON ch.id = l.channel_id
    WHERE l.budget_id IS NULL AND NOT l.outside_budget
    GROUP BY l.brand_id, br.name, l.market_id, mk.name, mk.sort_order, l.channel_id, ch.name, l.currency
    ORDER BY mk.sort_order, br.name, ch.name, l.currency
"""

_OUTSIDE_BUDGET_SQL = f"""
    WITH {_LINES_CTE}
    SELECT l.currency, {_SPENT}, {_EXPECTED}, COUNT(DISTINCT l.initiative_id) AS initiative_count
    FROM lines l
    WHERE l.outside_budget
    GROUP BY l.currency
    ORDER BY l.currency
"""

# $2 = the budget, $3 = its currency (listed first)
_BUDGET_SPEND_SQL = f"""
    WITH {_LINES_CTE}
    SELECT l.initiative_id, i.name AS initiative_name, ch.name AS channel_name, sc.name AS sub_channel_name,
           ed.event_code, l.currency, {_SPENT}, {_EXPECTED}, COUNT(*) AS line_count
    FROM lines l
    JOIN initiatives i ON i.id = l.initiative_id
    JOIN channels ch ON ch.id = i.channel_id
    LEFT JOIN sub_channels sc ON sc.id = i.sub_channel_id
    LEFT JOIN event_details ed ON ed.initiative_id = i.id
    WHERE l.budget_id = $2
    GROUP BY l.initiative_id, i.name, ch.name, sc.name, ed.event_code, l.currency
    ORDER BY l.currency <> $3, expected DESC, i.name
"""


# --- Budget rows ---

def label_sql(b: str, brand: str, market: str, channel: str) -> str:
    """'Aug 2026 · TBD/All · London' (and ' · Paid Search' when the budget is for one channel)."""
    return (
        f"to_char({b}.month, 'Mon YYYY') || ' · ' || COALESCE({brand}.name, 'TBD/All') || ' · ' || {market}.name"
        f" || COALESCE(' · ' || {channel}.name, '')"
    )


_BUDGET_SELECT = """
    SELECT b.id, b.month, b.brand_id, br.name AS brand_name, b.market_id, mk.name AS market_name,
           b.channel_id, ch.name AS channel_name, b.amount, b.currency, b.status,
           -- The month has ended (by the database's date)
           (b.month + interval '1 month')::date <= CURRENT_DATE AS month_ended,
           COALESCE((SELECT SUM(t.amount) FROM budget_transfers t WHERE t.to_budget_id = b.id), 0) AS moved_in,
           COALESCE((SELECT SUM(t.amount) FROM budget_transfers t WHERE t.from_budget_id = b.id), 0) AS moved_out,
           (SELECT COUNT(*) FROM budget_comments c WHERE c.budget_id = b.id) AS comment_count,
           lc.body AS latest_comment_body, lcu.full_name AS latest_comment_author, lc.created_at AS latest_comment_at,
           lu.at AS last_update, luu.full_name AS last_update_by_name
    FROM monthly_budgets b
    LEFT JOIN brands br ON br.id = b.brand_id
    JOIN markets mk ON mk.id = b.market_id
    LEFT JOIN channels ch ON ch.id = b.channel_id
    LEFT JOIN LATERAL (
        SELECT c.body, c.author_id, c.created_at FROM budget_comments c
        WHERE c.budget_id = b.id ORDER BY c.created_at DESC LIMIT 1
    ) lc ON true
    LEFT JOIN users lcu ON lcu.id = lc.author_id
    -- Last change: an edit, a comment or a money move, whichever is latest
    LEFT JOIN LATERAL (
        SELECT x.at, x.by FROM (
            SELECT b.updated_at AS at, b.updated_by AS by
            UNION ALL
            SELECT c.created_at, c.author_id FROM budget_comments c WHERE c.budget_id = b.id
            UNION ALL
            SELECT t.created_at, t.created_by FROM budget_transfers t WHERE t.from_budget_id = b.id OR t.to_budget_id = b.id
        ) x ORDER BY x.at DESC LIMIT 1
    ) lu ON true
    LEFT JOIN users luu ON luu.id = lu.by
"""
_BUDGET_ORDER = "ORDER BY mk.sort_order, b.brand_id IS NULL, br.name, b.channel_id IS NULL, ch.name, b.currency"

_COMMENT_SELECT = """
    SELECT c.id, c.budget_id, c.month, c.body, c.author_id, u.full_name AS author_name, c.created_at
    FROM budget_comments c
    LEFT JOIN users u ON u.id = c.author_id
"""


def _money(value) -> float:
    return round(float(value), 2)


def _progress(row, spent: Decimal) -> str:
    if row["status"] in ("CANCELLED", "NOT_COMMITTED"):
        return row["status"]
    if row["month_ended"]:
        return "COMPLETE"
    return "IN_PROGRESS" if spent > 0 else "NOT_STARTED"


def _shape_budget(row, figures: dict[str, tuple[Decimal, Decimal]]) -> dict:
    """A budget as the API returns it. figures: currency → (spent, expected) for the costs counted against it."""
    zero = Decimal(0)
    spent, expected = figures.get(row["currency"], (zero, zero))
    available = row["amount"] + row["moved_in"] - row["moved_out"]
    remaining = available - spent
    latest = None
    if row["latest_comment_at"] is not None:
        latest = {
            "body": row["latest_comment_body"],
            "author_name": row["latest_comment_author"],
            "created_at": row["latest_comment_at"],
        }
    return {
        "id": row["id"],
        "month": row["month"],
        "brand_id": row["brand_id"],
        "brand_name": row["brand_name"],
        "market_id": row["market_id"],
        "market_name": row["market_name"],
        "channel_id": row["channel_id"],
        "channel_name": row["channel_name"],
        "amount": _money(row["amount"]),
        "currency": row["currency"],
        "status": row["status"],
        "progress": _progress(row, spent),
        "moved_in": _money(row["moved_in"]),
        "moved_out": _money(row["moved_out"]),
        "available": _money(available),
        "spent": _money(spent),
        "expected": _money(expected),
        "remaining": _money(remaining),
        "percent_used": round(float(spent) / float(available) * 100, 1) if available else None,
        "over_budget": spent > available,
        "other_currency": [
            {"currency": code, "spent": _money(s), "expected": _money(e)}
            for code, (s, e) in sorted(figures.items())
            if code != row["currency"]
        ],
        "comment_count": row["comment_count"],
        "latest_comment": latest,
        "last_update": row["last_update"],
        "last_update_by_name": row["last_update_by_name"],
    }


def _figures_by_budget(rows) -> dict[UUID, dict[str, tuple[Decimal, Decimal]]]:
    by_budget: dict[UUID, dict[str, tuple[Decimal, Decimal]]] = defaultdict(dict)
    for r in rows:
        by_budget[r["budget_id"]][r["currency"]] = (r["spent"], r["expected"])
    return by_budget


async def _budget_row(conn, budget_id: UUID) -> dict:
    row = await conn.fetchrow(f"{_BUDGET_SELECT} WHERE b.id = $1", budget_id)
    if row is None:
        raise BudgetNotFoundError
    figures = await conn.fetch(
        f"SELECT * FROM ({_BUDGET_FIGURES_SQL}) f WHERE f.budget_id = $2", row["month"], budget_id
    )
    return _shape_budget(row, _figures_by_budget(figures)[budget_id])


def _previous_month(month: date) -> date:
    return date(month.year - 1, 12, 1) if month.month == 1 else date(month.year, month.month - 1, 1)


async def get_month(month: date) -> dict:
    """Every budget in a month with what's been spent, totals per currency, and spend no budget covers."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"{_BUDGET_SELECT} WHERE b.month = $1 {_BUDGET_ORDER}", month)
        figures = _figures_by_budget(await conn.fetch(_BUDGET_FIGURES_SQL, month))
        unbudgeted = await conn.fetch(_UNBUDGETED_SQL, month)
        outside = await conn.fetch(_OUTSIDE_BUDGET_SQL, month)
        notes = await conn.fetch(
            f"{_COMMENT_SELECT} WHERE c.budget_id IS NULL AND c.month = $1 ORDER BY c.created_at DESC", month
        )
        previous = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM monthly_budgets WHERE month = $1 AND status <> 'CANCELLED')",
            _previous_month(month),
        )

    budgets = [_shape_budget(r, figures.get(r["id"], {})) for r in rows]

    totals: dict[str, dict[str, float]] = {}
    for budget in budgets:
        if budget["status"] == "CANCELLED":
            continue
        total = totals.setdefault(budget["currency"], dict.fromkeys(("amount", "available", "spent", "expected", "remaining"), 0.0))
        for key in total:
            total[key] += budget[key]

    return {
        "month": month,
        "budgets": budgets,
        "totals": [
            {"currency": code, **{k: round(v, 2) for k, v in sums.items()}}
            for code, sums in sorted(totals.items())
        ],
        "unbudgeted": [
            {**dict(r), "spent": _money(r["spent"]), "expected": _money(r["expected"])} for r in unbudgeted
        ],
        "outside_budget": [
            {**dict(r), "spent": _money(r["spent"]), "expected": _money(r["expected"])} for r in outside
        ],
        "notes": [dict(n) for n in notes],
        "previous_month_has_budgets": previous,
    }


async def get_budget(budget_id: UUID) -> dict:
    """One budget with the costs counted against it, its money moves and its comments."""
    pool = get_pool()
    async with pool.acquire() as conn:
        budget = await _budget_row(conn, budget_id)
        spend = await conn.fetch(_BUDGET_SPEND_SQL, budget["month"], budget_id, budget["currency"])
        transfers = await conn.fetch(
            f"""
            SELECT t.id,
                   CASE WHEN t.to_budget_id = $1 THEN 'IN' ELSE 'OUT' END AS direction,
                   o.id AS other_budget_id, {label_sql('o', 'ob', 'om', 'oc')} AS other_budget_label,
                   t.amount::float8 AS amount, t.note, t.created_at, u.full_name AS created_by_name
            FROM budget_transfers t
            JOIN monthly_budgets o ON o.id = CASE WHEN t.to_budget_id = $1 THEN t.from_budget_id ELSE t.to_budget_id END
            LEFT JOIN brands ob ON ob.id = o.brand_id
            JOIN markets om ON om.id = o.market_id
            LEFT JOIN channels oc ON oc.id = o.channel_id
            LEFT JOIN users u ON u.id = t.created_by
            WHERE t.from_budget_id = $1 OR t.to_budget_id = $1
            ORDER BY t.created_at DESC
            """,
            budget_id,
        )
        comments = await conn.fetch(f"{_COMMENT_SELECT} WHERE c.budget_id = $1 ORDER BY c.created_at DESC", budget_id)

    return {
        **budget,
        "spend": [{**dict(r), "spent": _money(r["spent"]), "expected": _money(r["expected"])} for r in spend],
        "transfers": [dict(t) for t in transfers],
        "comments": [dict(c) for c in comments],
    }


# --- Budget writes ---

async def _normalise_brand(conn, brand_id: int | None) -> int | None:
    """The TBD/All brand from the brand list is stored as no brand, so there's one way to say TBD/All."""
    if brand_id is None:
        return None
    tbd = await conn.fetchval("SELECT id FROM brands WHERE code = $1", TBD_ALL_BRAND)
    return None if brand_id == tbd else brand_id


async def _duplicate_error(conn, key: dict) -> BudgetValidationError:
    names = await conn.fetchrow(
        """
        SELECT (SELECT name FROM brands WHERE id = $1) AS brand, (SELECT name FROM markets WHERE id = $2) AS market,
               (SELECT name FROM channels WHERE id = $3) AS channel
        """,
        key["brand_id"], key["market_id"], key["channel_id"],
    )
    parts = [names["brand"] or "TBD/All", names["market"]] + ([names["channel"]] if names["channel"] else [])
    return BudgetValidationError(f"There's already a budget for {' · '.join(parts)} in {key['month'].strftime('%B %Y')}.")


async def _translate_db_error(conn, exc: asyncpg.PostgresError, key: dict) -> BudgetValidationError:
    constraint = getattr(exc, "constraint_name", None)
    if constraint == "monthly_budgets_key":
        return await _duplicate_error(conn, key)
    return BudgetValidationError(_CONSTRAINT_MESSAGES.get(constraint, "Invalid budget details."))


async def create_budget(values: dict, actor_id: UUID) -> dict:
    """Add a budget for a month and audit it, in one transaction."""
    pool = get_pool()
    async with pool.acquire() as conn:
        values = {**values, "brand_id": await _normalise_brand(conn, values["brand_id"])}
        try:
            async with conn.transaction():
                budget_id = await conn.fetchval(
                    """
                    INSERT INTO monthly_budgets (month, brand_id, market_id, channel_id, amount, currency, status,
                                                 created_by, updated_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8)
                    RETURNING id
                    """,
                    values["month"], values["brand_id"], values["market_id"], values["channel_id"],
                    values["amount"], values["currency"], values["status"], actor_id,
                )
                await log_audit(conn, actor_id, "CREATE", BUDGET_ENTITY, budget_id, new_value=values)
        except (asyncpg.CheckViolationError, asyncpg.ForeignKeyViolationError, asyncpg.UniqueViolationError) as exc:
            raise await _translate_db_error(conn, exc, values) from exc
        return await _budget_row(conn, budget_id)


async def update_budget(budget_id: UUID, changes: dict, actor_id: UUID) -> dict:
    """Apply a partial update and audit what changed (old and new values), in one transaction."""
    pool = get_pool()
    async with pool.acquire() as conn:
        if "brand_id" in changes:
            changes = {**changes, "brand_id": await _normalise_brand(conn, changes["brand_id"])}
        key: dict = {}
        try:
            async with conn.transaction():
                current = await conn.fetchrow(
                    f"SELECT month, {', '.join(_UPDATABLE_COLUMNS)} FROM monthly_budgets WHERE id = $1 FOR UPDATE",
                    budget_id,
                )
                if current is None:
                    raise BudgetNotFoundError
                current = {**dict(current), "amount": float(current["amount"])}
                changed = {k: v for k, v in changes.items() if k in _UPDATABLE_COLUMNS and current[k] != v}
                key = {k: changed.get(k, current[k]) for k in _KEY_COLUMNS}

                if "currency" in changed and await _has_transfers(conn, budget_id):
                    raise BudgetValidationError("Remove the money moves on this budget before changing its currency.")

                if changed:
                    columns = [c for c in _UPDATABLE_COLUMNS if c in changed]
                    assignments = ", ".join(f"{c} = ${n}" for n, c in enumerate(columns, start=2))
                    await conn.execute(
                        f"UPDATE monthly_budgets SET {assignments}, updated_at = now(), updated_by = ${len(columns) + 2} "
                        "WHERE id = $1",
                        budget_id, *[changed[c] for c in columns], actor_id,
                    )
                    # The budget's key in both, so the audit log can name it whatever changed
                    old_key = {k: current[k] for k in _KEY_COLUMNS}
                    await log_audit(
                        conn, actor_id, "UPDATE", BUDGET_ENTITY, budget_id,
                        old_value={**old_key, **{c: current[c] for c in columns}},
                        new_value={**key, **changed},
                    )
        except (asyncpg.CheckViolationError, asyncpg.ForeignKeyViolationError, asyncpg.UniqueViolationError) as exc:
            raise await _translate_db_error(conn, exc, key) from exc
        return await _budget_row(conn, budget_id)


async def _has_transfers(conn, budget_id: UUID) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM budget_transfers WHERE from_budget_id = $1 OR to_budget_id = $1)", budget_id
    )


async def delete_budget(budget_id: UUID, actor_id: UUID) -> None:
    """Delete a budget (its comments go with it) and audit what it was. Refused while money moves use it."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT month, brand_id, market_id, channel_id, amount::float8 AS amount, currency, status "
                "FROM monthly_budgets WHERE id = $1 FOR UPDATE",
                budget_id,
            )
            if current is None:
                raise BudgetNotFoundError
            if await _has_transfers(conn, budget_id):
                raise BudgetValidationError("Remove the money moves on this budget first.")
            await conn.execute("DELETE FROM monthly_budgets WHERE id = $1", budget_id)
            await log_audit(conn, actor_id, "DELETE", BUDGET_ENTITY, budget_id, old_value=dict(current))


async def copy_month(from_month: date, to_month: date, actor_id: UUID) -> dict:
    """Copy a month's budgets (not cancelled ones) into another month as Not committed.

    Budgets that already exist in the target month are left alone and counted as skipped. One statement
    inserts the budgets and their audit entries.
    """
    if from_month == to_month:
        raise BudgetValidationError("Choose a different month to copy to.")
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                WITH source AS (
                    SELECT brand_id, market_id, channel_id, amount, currency
                    FROM monthly_budgets WHERE month = $1 AND status <> 'CANCELLED'
                ),
                inserted AS (
                    INSERT INTO monthly_budgets (month, brand_id, market_id, channel_id, amount, currency, status,
                                                 created_by, updated_by)
                    SELECT $2, brand_id, market_id, channel_id, amount, currency, 'NOT_COMMITTED', $3, $3 FROM source
                    ON CONFLICT DO NOTHING
                    RETURNING id, month, brand_id, market_id, channel_id, amount, currency, status
                ),
                audited AS (
                    INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, new_value, created_at)
                    SELECT $3, 'CREATE', '{BUDGET_ENTITY}', CAST(id AS TEXT),
                           jsonb_build_object('month', month, 'brand_id', brand_id, 'market_id', market_id,
                                              'channel_id', channel_id, 'amount', amount::float8, 'currency', currency,
                                              'status', status, 'copied_from', $1::date),
                           now()
                    FROM inserted
                    RETURNING 1
                )
                SELECT (SELECT COUNT(*) FROM source) AS total, (SELECT COUNT(*) FROM audited) AS created
                """,
                from_month, to_month, actor_id,
            )
    return {"created": row["created"], "skipped": row["total"] - row["created"]}


# --- Money moves ---

async def _transfer(conn, transfer_id: UUID) -> dict:
    row = await conn.fetchrow(
        """
        SELECT t.id, t.from_budget_id, t.to_budget_id, t.amount::float8 AS amount, t.note, t.created_at,
               u.full_name AS created_by_name
        FROM budget_transfers t LEFT JOIN users u ON u.id = t.created_by
        WHERE t.id = $1
        """,
        transfer_id,
    )
    return dict(row)


async def create_transfer(values: dict, actor_id: UUID) -> dict:
    """Move money from one budget to another (same currency) and audit it."""
    if values["from_budget_id"] == values["to_budget_id"]:
        raise BudgetValidationError("Choose two different budgets.")
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                budgets = await conn.fetch(
                    "SELECT id, currency FROM monthly_budgets WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE",
                    [values["from_budget_id"], values["to_budget_id"]],
                )
                if len(budgets) != 2:
                    raise BudgetValidationError("That budget no longer exists.")
                if budgets[0]["currency"] != budgets[1]["currency"]:
                    raise BudgetValidationError("Money can only move between budgets in the same currency.")
                transfer_id = await conn.fetchval(
                    """
                    INSERT INTO budget_transfers (from_budget_id, to_budget_id, amount, note, created_by)
                    VALUES ($1, $2, $3, $4, $5) RETURNING id
                    """,
                    values["from_budget_id"], values["to_budget_id"], values["amount"], values["note"], actor_id,
                )
                await log_audit(
                    conn, actor_id, "CREATE", TRANSFER_ENTITY, transfer_id,
                    new_value={**values, "currency": budgets[0]["currency"]},
                )
        except asyncpg.CheckViolationError as exc:
            raise BudgetValidationError(
                _CONSTRAINT_MESSAGES.get(getattr(exc, "constraint_name", None), "Invalid money move.")
            ) from exc
        return await _transfer(conn, transfer_id)


async def delete_transfer(transfer_id: UUID, actor_id: UUID) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "DELETE FROM budget_transfers WHERE id = $1 "
                "RETURNING from_budget_id, to_budget_id, amount::float8 AS amount, note",
                transfer_id,
            )
            if current is None:
                raise BudgetNotFoundError
            await log_audit(conn, actor_id, "DELETE", TRANSFER_ENTITY, transfer_id, old_value=dict(current))


# --- Comments ---

async def create_comment(values: dict, actor_id: UUID) -> dict:
    """Comment on a budget (in its month) or on a whole month."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            month = values["month"]
            if values["budget_id"] is not None:
                month = await conn.fetchval("SELECT month FROM monthly_budgets WHERE id = $1", values["budget_id"])
                if month is None:
                    raise BudgetNotFoundError
            comment_id = await conn.fetchval(
                "INSERT INTO budget_comments (budget_id, month, body, author_id) VALUES ($1, $2, $3, $4) RETURNING id",
                values["budget_id"], month, values["body"], actor_id,
            )
            await log_audit(
                conn, actor_id, "CREATE", COMMENT_ENTITY, comment_id,
                new_value={"budget_id": values["budget_id"], "month": month, "body": values["body"]},
            )
        row = await conn.fetchrow(f"{_COMMENT_SELECT} WHERE c.id = $1", comment_id)
    return dict(row)


async def delete_comment(comment_id: UUID, actor_id: UUID) -> None:
    """Only the person who wrote a comment (or a super admin) can delete it."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT budget_id, month, body, author_id FROM budget_comments WHERE id = $1 FOR UPDATE", comment_id
            )
            if current is None:
                raise BudgetNotFoundError
            if current["author_id"] != actor_id:
                is_admin = await conn.fetchval("SELECT is_super_admin FROM users WHERE id = $1", actor_id)
                if not is_admin:
                    raise BudgetPermissionError("Only the person who wrote a comment can delete it.")
            await conn.execute("DELETE FROM budget_comments WHERE id = $1", comment_id)
            await log_audit(conn, actor_id, "DELETE", COMMENT_ENTITY, comment_id, old_value=dict(current))


# --- Week view ---

# Every cost dated from $1 to $2, with the budget it counts against: the same rule as _LINES_CTE (keep them in step),
# with each cost matched in its own month, since a week can cross into the next month.
_WEEK_LINES_SQL = f"""
    SELECT s.id, s.incurred_on, s.description, s.vendor, s.status, s.amount, s.currency,
           sc.name AS category_name, i.id AS initiative_id, i.name AS initiative_name, ed.event_code,
           i.brand_id, br.name AS brand_name, mk.market_id, mkn.name AS market_name, i.channel_id, ch.name AS channel_name,
           s.source, s.invoice_ref, s.paid_on, s.reconciled_month, i.end_date AS initiative_end_date,
           bud.id AS budget_id, bud.label AS budget_label, bud.currency AS budget_currency, ob.outside_budget
    FROM spend_lines s
    JOIN initiatives i ON i.id = s.initiative_id
    JOIN brands br ON br.id = i.brand_id
    JOIN channels ch ON ch.id = i.channel_id
    LEFT JOIN spend_categories sc ON sc.id = s.category_id
    LEFT JOIN event_details ed ON ed.initiative_id = i.id
    CROSS JOIN LATERAL (
        SELECT COALESCE(i.market_id, (SELECT id FROM markets WHERE code = '{UNASSIGNED_MARKET}')) AS market_id
    ) mk
    JOIN markets mkn ON mkn.id = mk.market_id
    CROSS JOIN LATERAL (SELECT {_OUTSIDE_BUDGET} AS outside_budget) ob
    LEFT JOIN LATERAL (
        SELECT b.id, b.currency, {label_sql("b", "bbr", "bmk", "bch")} AS label
        FROM monthly_budgets b
        LEFT JOIN brands bbr ON bbr.id = b.brand_id
        JOIN markets bmk ON bmk.id = b.market_id
        LEFT JOIN channels bch ON bch.id = b.channel_id
        WHERE b.month = date_trunc('month', s.incurred_on)::date AND b.market_id = mk.market_id AND b.status <> 'CANCELLED'
          AND (b.brand_id = i.brand_id OR b.brand_id IS NULL)
          AND (b.channel_id = i.channel_id OR b.channel_id IS NULL)
          AND NOT ob.outside_budget
        ORDER BY b.brand_id IS NULL, b.channel_id IS NULL
        LIMIT 1
    ) bud ON true
    WHERE s.incurred_on BETWEEN $1 AND $2
    ORDER BY s.incurred_on, s.amount DESC, i.name
"""

# What the week's months have available, per currency (cancelled budgets left out, as in the month totals)
_MONTH_AVAILABLE_SQL = """
    SELECT b.month, b.currency,
           SUM(b.amount
               + COALESCE((SELECT SUM(t.amount) FROM budget_transfers t WHERE t.to_budget_id = b.id), 0)
               - COALESCE((SELECT SUM(t.amount) FROM budget_transfers t WHERE t.from_budget_id = b.id), 0)) AS available
    FROM monthly_budgets b
    WHERE b.month = ANY($1::date[]) AND b.status <> 'CANCELLED'
    GROUP BY b.month, b.currency
"""


def _month_start(day: date) -> date:
    return day.replace(day=1)


async def get_week(start: date) -> dict:
    """A Monday-to-Sunday week of costs, each on the day it's dated with the budget it counts against, plus how far
    through their budgets the week's months are by the end of the week."""
    end = start + timedelta(days=6)
    months = sorted({_month_start(start), _month_start(end)})
    pool = get_pool()
    async with pool.acquire() as conn:
        # From the start of the first month, so the month-to-date figures come from the same rows
        rows = await conn.fetch(_WEEK_LINES_SQL, months[0], end)
        available = await conn.fetch(_MONTH_AVAILABLE_SQL, months)

    lines = [dict(r) for r in rows if r["incurred_on"] >= start]

    week_totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"spent": Decimal(0), "expected": Decimal(0)})
    for line in lines:
        totals = week_totals[line["currency"]]
        totals["expected"] += line["amount"]
        if line["status"] in SPENT_STATUSES:
            totals["spent"] += line["amount"]

    # Spent against budgets by the end of the week, in each budget's own currency (like the month figures)
    spent_to_date: dict[tuple[date, str], Decimal] = defaultdict(Decimal)
    for r in rows:
        if r["budget_id"] is not None and r["status"] in SPENT_STATUSES and r["currency"] == r["budget_currency"]:
            spent_to_date[(_month_start(r["incurred_on"]), r["currency"])] += r["amount"]
    month_figures = []
    for r in sorted(available, key=lambda a: (a["month"], a["currency"])):
        month_end = (r["month"].replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        spent = spent_to_date[(r["month"], r["currency"])]
        month_figures.append({
            "month": r["month"],
            "currency": r["currency"],
            "available": _money(r["available"]),
            "spent_to_date": _money(spent),
            "through": min(end, month_end),
            "days_in_month": month_end.day,
        })

    return {
        "start": start,
        "end": end,
        "lines": [{**line, "amount": _money(line["amount"])} for line in lines],
        "totals": [
            {"currency": code, "spent": _money(t["spent"]), "expected": _money(t["expected"])}
            for code, t in sorted(week_totals.items())
        ],
        "months": month_figures,
    }


# --- A period of months (the Budgets & Expenses page) ---
# Every figure comes from the same cost lines as the week view (_WEEK_LINES_SQL), each matched to a budget in its
# own month, so a quarter or a year adds up exactly like its months do. Three queries, however long the period.

COMMITTED_STATUSES = ("COMMITTED",)


def _month_end(month: date) -> date:
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


async def _period_lines(conn, start: date, end: date):
    return await conn.fetch(_WEEK_LINES_SQL, start, end)


async def get_period(start: date, end: date) -> dict:
    """Budgets from the month of start to the month of end, with what was spent and committed against each,
    totals per currency (approved budgets apart from ones awaiting approval), month-by-month figures, spend by
    channel, spend no budget covers, and travel (outside marketing budgets)."""
    first, last = _month_start(start), _month_start(end)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await _period_lines(conn, first, _month_end(last))
        budget_rows = await conn.fetch(f"{_BUDGET_SELECT} WHERE b.month BETWEEN $1 AND $2 {_BUDGET_ORDER}, b.month", first, last)

    zero = Decimal(0)
    # Per budget and currency: [spent, expected]; committed only in the budget's own currency
    by_budget: dict[UUID, dict[str, list[Decimal]]] = defaultdict(lambda: defaultdict(lambda: [Decimal(0), Decimal(0)]))
    committed: dict[UUID, Decimal] = defaultdict(Decimal)
    channels: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(lambda: {"spent": Decimal(0), "committed": Decimal(0)})
    unbudgeted: dict[tuple, dict] = {}
    outside: dict[str, dict] = {}
    for r in rows:
        actual = r["status"] in SPENT_STATUSES
        if r["outside_budget"]:
            o = outside.setdefault(r["currency"], {"currency": r["currency"], "spent": zero, "expected": zero, "initiatives": set()})
            o["expected"] += r["amount"]
            o["spent"] += r["amount"] if actual else zero
            o["initiatives"].add(r["initiative_id"])
            continue
        c = channels[(r["channel_name"], r["currency"])]
        c["spent"] += r["amount"] if actual else zero
        c["committed"] += r["amount"] if r["status"] in COMMITTED_STATUSES else zero
        if r["budget_id"] is not None:
            figures = by_budget[r["budget_id"]][r["currency"]]
            figures[1] += r["amount"]
            figures[0] += r["amount"] if actual else zero
            if r["status"] in COMMITTED_STATUSES and r["currency"] == r["budget_currency"]:
                committed[r["budget_id"]] += r["amount"]
        else:
            key = (r["brand_id"], r["market_id"], r["channel_id"], r["currency"])
            u = unbudgeted.setdefault(key, {
                "brand_id": r["brand_id"], "brand_name": r["brand_name"], "market_id": r["market_id"],
                "market_name": r["market_name"], "channel_id": r["channel_id"], "channel_name": r["channel_name"],
                "currency": r["currency"], "spent": zero, "expected": zero, "initiatives": set(),
            })
            u["expected"] += r["amount"]
            u["spent"] += r["amount"] if actual else zero
            u["initiatives"].add(r["initiative_id"])

    budgets = []
    for row in budget_rows:
        figures = {code: (f[0], f[1]) for code, f in by_budget.get(row["id"], {}).items()}
        budgets.append({**_shape_budget(row, figures), "committed": _money(committed.get(row["id"], zero))})

    # Approved budgets make the totals; ones awaiting approval are counted apart, cancelled ones not at all
    totals: dict[str, dict[str, float]] = {}
    months: dict[tuple[date, str], dict[str, float]] = {}
    keys = ("amount", "available", "spent", "committed", "expected", "remaining")
    for b in budgets:
        if b["status"] == "CANCELLED":
            continue
        total = totals.setdefault(b["currency"], {**dict.fromkeys(keys, 0.0), "pending_amount": 0.0, "pending_count": 0, "budget_count": 0})
        if b["status"] == "NOT_COMMITTED":
            total["pending_amount"] += b["amount"]
            total["pending_count"] += 1
            continue
        total["budget_count"] += 1
        for k in keys:
            total[k] += b[k]
        m = months.setdefault((b["month"], b["currency"]), dict.fromkeys(("available", "spent", "committed"), 0.0))
        for k in m:
            m[k] += b[k]

    def money_rows(items):
        return [{**{k: v for k, v in i.items() if k != "initiatives"}, "spent": _money(i["spent"]), "expected": _money(i["expected"]),
                 "initiative_count": len(i["initiatives"])} for i in items]

    return {
        "from_month": first,
        "to_month": last,
        "budgets": budgets,
        "totals": [
            {"currency": code, **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in t.items()}}
            for code, t in sorted(totals.items())
        ],
        "months": [
            {"month": month, "currency": code, **{k: round(v, 2) for k, v in m.items()}}
            for (month, code), m in sorted(months.items())
        ],
        "channels": [
            {"channel_name": name, "currency": code, "spent": _money(c["spent"]), "committed": _money(c["committed"])}
            for (name, code), c in sorted(channels.items(), key=lambda kv: -kv[1]["spent"])
            if c["spent"] or c["committed"]
        ],
        "unbudgeted": sorted(money_rows(unbudgeted.values()), key=lambda u: (u["market_name"], u["brand_name"], u["channel_name"])),
        "outside_budget": money_rows(sorted(outside.values(), key=lambda o: o["currency"])),
    }


MAX_EXPENSES = 5000


async def list_expenses(start: date, end: date) -> dict:
    """Every cost dated from start to end, with where it came from, its stage, whether Finance reconciled it, and
    the budget it counts against (or none, or outside budgets for travel). At most MAX_EXPENSES, newest first."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await _period_lines(conn, start, end)
    lines = [{**dict(r), "amount": _money(r["amount"])} for r in reversed(rows)]
    return {"from_date": start, "to_date": end, "lines": lines[:MAX_EXPENSES], "truncated": len(lines) > MAX_EXPENSES}
