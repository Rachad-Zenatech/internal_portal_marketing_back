"""Marketing initiatives — one record per initiative, whatever the channel."""
from postgresql_db.database import get_pool


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
        SELECT i.id, i.name,
               i.brand_id, b.name AS brand_name,
               i.channel_id, c.name AS channel_name,
               i.sub_channel_id, s.name AS sub_channel_name,
               i.owner_user_id, u.full_name AS owner_name,
               i.start_date, i.end_date,
               i.budget_amount::float8 AS budget_amount, i.currency, i.status,
               COUNT(*) OVER () AS total_count
        FROM initiatives i
        JOIN brands b ON b.id = i.brand_id
        JOIN channels c ON c.id = i.channel_id
        LEFT JOIN sub_channels s ON s.id = i.sub_channel_id
        LEFT JOIN users u ON u.id = i.owner_user_id
        {where}
        ORDER BY i.start_date DESC NULLS LAST, i.name
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
