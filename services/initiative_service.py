"""Marketing initiatives — one record per initiative, whatever the channel."""
from uuid import UUID

import asyncpg

from models.initiative_model import InitiativeCreate
from postgresql_db.database import get_pool
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
           i.start_date, i.end_date,
           i.budget_amount::float8 AS budget_amount, i.currency, i.status
    FROM initiatives i
    JOIN brands b ON b.id = i.brand_id
    JOIN channels c ON c.id = i.channel_id
    LEFT JOIN sub_channels s ON s.id = i.sub_channel_id
    LEFT JOIN funnel_stages fs ON fs.id = i.funnel_stage_id
    LEFT JOIN goals g ON g.id = i.goal_id
    LEFT JOIN primary_kpis k ON k.id = i.primary_kpi_id
    LEFT JOIN users u ON u.id = i.owner_user_id
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
    "initiatives_primary_kpi_id_fkey": "Unknown primary KPI.",
    "initiatives_owner_user_id_fkey": "Unknown owner.",
    "initiatives_dates_check": "End date can't be before the start date.",
    "initiatives_budget_check": "Budget can't be negative.",
    "initiatives_status_check": "Unknown status.",
}


class InitiativeValidationError(Exception):
    """A request that breaks a business rule; the message is safe to show to users."""


async def list_initiatives(
    *,
    channel_id: int | None = None,
    sub_channel_id: int | None = None,
    brand_id: int | None = None,
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
    if status:
        add("i.status = ${n}", status)
    if search and search.strip():
        add("i.name ILIKE ${n}", f"%{search.strip()}%")

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
            total = await conn.fetchval(f"SELECT count(*) FROM initiatives i {where}", *args[:-2])

    return [{k: v for k, v in dict(r).items() if k != "total_count"} for r in rows], total


async def create_initiative(data: InitiativeCreate, actor_id: UUID) -> dict:
    """Insert an initiative and its audit entry in one transaction; return it as the list shows it."""
    values = data.model_dump()
    values["owner_user_id"] = values["owner_user_id"] or actor_id

    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                initiative_id = await conn.fetchval(
                    """
                    INSERT INTO initiatives (name, brand_id, channel_id, sub_channel_id,
                                             funnel_stage_id, goal_id, primary_kpi_id, owner_user_id,
                                             start_date, end_date, budget_amount, currency, status,
                                             created_by, updated_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $14)
                    RETURNING id
                    """,
                    values["name"], values["brand_id"], values["channel_id"], values["sub_channel_id"],
                    values["funnel_stage_id"], values["goal_id"], values["primary_kpi_id"], values["owner_user_id"],
                    values["start_date"], values["end_date"], values["budget_amount"], values["currency"],
                    values["status"], actor_id,
                )
                await log_audit(conn, actor_id, "CREATE", AUDIT_ENTITY, initiative_id, new_value=values)
        except (asyncpg.ForeignKeyViolationError, asyncpg.CheckViolationError) as exc:
            message = _CONSTRAINT_MESSAGES.get(exc.constraint_name, "Invalid initiative details.")
            raise InitiativeValidationError(message) from exc

        row = await conn.fetchrow(f"{_SELECT} WHERE i.id = $1", initiative_id)
    return dict(row)


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
