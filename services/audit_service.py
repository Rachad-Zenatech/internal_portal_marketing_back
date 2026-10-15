import json
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from postgresql_db.database import get_pool


def _budget_label(budget_id_text: str | None, key_json: str | None) -> str:
    """SQL for a budget's name, "Sep 2026 · Othership · London" (plus " · Paid Search" for one channel).

    From the budget row whose id (as text) is budget_id_text, or from the month, brand_id, market_id and
    channel_id recorded in the JSON key_json. NULL when neither is found.
    """
    if budget_id_text is not None:
        return f"""(SELECT to_char(b.month, 'Mon YYYY') || ' · ' || COALESCE(br.name, 'TBD/All') || ' · ' || mk.name
                       || COALESCE(' · ' || ch.name, '')
                FROM monthly_budgets b
                LEFT JOIN brands br ON br.id = b.brand_id
                JOIN markets mk ON mk.id = b.market_id
                LEFT JOIN channels ch ON ch.id = b.channel_id
                WHERE CAST(b.id AS TEXT) = {budget_id_text} LIMIT 1)"""
    return f"""(SELECT to_char(CAST(({key_json})->>'month' AS DATE), 'Mon YYYY') || ' · ' || COALESCE(br.name, 'TBD/All')
                       || ' · ' || mk.name || COALESCE(' · ' || ch.name, '')
                FROM markets mk
                LEFT JOIN brands br ON CAST(br.id AS TEXT) = ({key_json})->>'brand_id'
                LEFT JOIN channels ch ON CAST(ch.id AS TEXT) = ({key_json})->>'channel_id'
                WHERE CAST(mk.id AS TEXT) = ({key_json})->>'market_id' LIMIT 1)"""


def entity_title_sql(alias: str) -> str:
    """SQL expression giving a readable name for the audited item (e.g. the initiative's name).

    Initiatives fall back to the name recorded in the audit entry, so entries for a since-renamed
    or removed initiative still read sensibly.
    """
    return f"""
        CASE
            WHEN {alias}.entity_type = 'User' THEN (SELECT full_name FROM users WHERE CAST(id AS TEXT) = {alias}.entity_id LIMIT 1)
            WHEN {alias}.entity_type = 'Role' THEN (SELECT name FROM roles WHERE CAST(id AS TEXT) = {alias}.entity_id LIMIT 1)
            WHEN {alias}.entity_type = 'Initiative' THEN COALESCE(
                (SELECT name FROM initiatives WHERE CAST(id AS TEXT) = {alias}.entity_id LIMIT 1),
                {alias}.new_value->>'name',
                {alias}.old_value->>'name'
            )
            -- "Booth space · EVT-2026-0007" (the initiative's name when it isn't an event). Deleted costs fall back
            -- to the category and initiative recorded in the entry; a deleted initiative leaves just the category.
            WHEN {alias}.entity_type = 'SpendLine' THEN COALESCE(
                (SELECT sc.name || ' · ' || COALESCE(ed.event_code, i.name)
                 FROM spend_lines s
                 JOIN spend_categories sc ON sc.id = s.category_id
                 JOIN initiatives i ON i.id = s.initiative_id
                 LEFT JOIN event_details ed ON ed.initiative_id = i.id
                 WHERE CAST(s.id AS TEXT) = {alias}.entity_id LIMIT 1),
                (SELECT sc.name || COALESCE(' · ' || COALESCE(ed.event_code, i.name), '')
                 FROM spend_categories sc
                 LEFT JOIN initiatives i
                        ON CAST(i.id AS TEXT) = COALESCE({alias}.old_value->>'initiative_id', {alias}.new_value->>'initiative_id')
                 LEFT JOIN event_details ed ON ed.initiative_id = i.id
                 WHERE CAST(sc.id AS TEXT) = COALESCE({alias}.old_value->>'category_id', {alias}.new_value->>'category_id')
                 LIMIT 1)
            )
            -- "Booth demo · Acme Facilities · EVT-2026-0007"; deleted demos fall back to the event in the entry
            WHEN {alias}.entity_type = 'Demo' THEN COALESCE(
                (SELECT dt.name || COALESCE(' · ' || d.company, '') || ' · ' || COALESCE(ed.event_code, i.name)
                 FROM event_demos d
                 JOIN demo_types dt ON dt.id = d.demo_type_id
                 JOIN initiatives i ON i.id = d.initiative_id
                 LEFT JOIN event_details ed ON ed.initiative_id = i.id
                 WHERE CAST(d.id AS TEXT) = {alias}.entity_id LIMIT 1),
                (SELECT 'Demo' || COALESCE(' · ' || COALESCE({alias}.old_value->>'company', {alias}.new_value->>'company'), '')
                        || COALESCE(' · ' || COALESCE(ed.event_code, i.name), '')
                 FROM (SELECT 1) one
                 LEFT JOIN initiatives i
                        ON CAST(i.id AS TEXT) = COALESCE({alias}.old_value->>'initiative_id', {alias}.new_value->>'initiative_id')
                 LEFT JOIN event_details ed ON ed.initiative_id = i.id)
            )
            -- Results and success settings are recorded against the initiative itself: "Results · EVT-2026-0007"
            WHEN {alias}.entity_type IN ('Outcomes', 'Evaluation') THEN
                (CASE WHEN {alias}.entity_type = 'Outcomes' THEN 'Results' ELSE 'Success settings' END)
                || COALESCE(' · ' || (SELECT COALESCE(ed.event_code, i.name)
                                      FROM initiatives i LEFT JOIN event_details ed ON ed.initiative_id = i.id
                                      WHERE CAST(i.id AS TEXT) = {alias}.entity_id LIMIT 1), '')
            -- "Decision: Scale · EVT-2026-0007"
            WHEN {alias}.entity_type = 'Decision' THEN
                (SELECT 'Decision: ' || s.name || ' · ' || COALESCE(ed.event_code, i.name)
                 FROM initiatives i
                 LEFT JOIN event_details ed ON ed.initiative_id = i.id
                 LEFT JOIN scale_or_cut_options s
                        ON CAST(s.id AS TEXT) = COALESCE({alias}.new_value->>'scale_or_cut_id', {alias}.old_value->>'scale_or_cut_id')
                 WHERE CAST(i.id AS TEXT) = COALESCE({alias}.new_value->>'initiative_id', {alias}.old_value->>'initiative_id')
                 LIMIT 1)
            -- "Jane Smith · EVT-2026-0007": who was put on (or taken off) which event
            WHEN {alias}.entity_type = 'EventStaff' THEN
                (SELECT COALESCE(u.full_name, u.email, 'Staff') || COALESCE(' · ' || COALESCE(ed.event_code, i.name), '')
                 FROM (SELECT 1) one
                 LEFT JOIN users u
                        ON CAST(u.id AS TEXT) = COALESCE((SELECT CAST(s.user_id AS TEXT) FROM event_staff s WHERE CAST(s.id AS TEXT) = {alias}.entity_id),
                                                         {alias}.old_value->>'user_id', {alias}.new_value->>'user_id')
                 LEFT JOIN initiatives i
                        ON CAST(i.id AS TEXT) = COALESCE({alias}.new_value->>'initiative_id', {alias}.old_value->>'initiative_id')
                 LEFT JOIN event_details ed ON ed.initiative_id = i.id)
            -- Spreadsheet uploads: the file name
            WHEN {alias}.entity_type = 'ImportBatch' THEN COALESCE(
                (SELECT filename FROM import_batches WHERE CAST(id AS TEXT) = {alias}.entity_id LIMIT 1),
                {alias}.new_value->>'filename',
                {alias}.old_value->>'filename'
            )
            -- "Budget · Sep 2026 · Othership · London"; deleted budgets fall back to the key recorded in the entry
            WHEN {alias}.entity_type = 'MonthlyBudget' THEN 'Budget' || COALESCE(' · ' || COALESCE(
                {_budget_label(f"{alias}.entity_id", None)},
                {_budget_label(None, f"COALESCE({alias}.new_value, {alias}.old_value)")}
            ), '')
            -- "Move money to Sep 2026 · Othership · London"
            WHEN {alias}.entity_type = 'BudgetTransfer' THEN 'Move money' || COALESCE(' to ' || {_budget_label(
                f"COALESCE({alias}.new_value->>'to_budget_id', {alias}.old_value->>'to_budget_id')", None)}, '')
            -- "Comment · Sep 2026 · Othership · London", or "Comment · Sep 2026" for a note on the whole month
            WHEN {alias}.entity_type = 'BudgetComment' THEN 'Comment · ' || COALESCE(
                {_budget_label(f"COALESCE({alias}.new_value->>'budget_id', {alias}.old_value->>'budget_id')", None)},
                to_char(CAST(COALESCE({alias}.new_value->>'month', {alias}.old_value->>'month') AS DATE), 'Mon YYYY')
            )
            ELSE NULL
        END
    """


async def log_audit_event(
    entity_type: str,
    entity_id: str,
    action: str,
    user_id: UUID,
    changes: Optional[Dict[str, Any]] = None
):
    """
    Records an event in the audit_logs table.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_logs (entity_type, entity_id, action, actor_user_id, new_value)
            VALUES ($1, $2, $3, $4, $5)
            """,
            entity_type,
            entity_id,
            action,
            user_id,
            json.dumps(changes, default=str) if changes else None
        )

async def get_audit_logs(
    limit: int = 50,
    offset: int = 0,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    action: Optional[str] = None,
    search: Optional[str] = None,
    user_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], int]:
    pool = get_pool()
    async with pool.acquire() as conn:
        query = f"""
        SELECT al.*,
               COALESCE(u.full_name, 'System') as actor_name,
               u.email as actor_email,
               u.department as actor_department,
               u.job_title as actor_job_title,
               {entity_title_sql("al")} as entity_title
        FROM audit_logs al
        LEFT JOIN users u ON al.actor_user_id = u.id
        WHERE 1=1
        """
        count_query = """
        SELECT count(*)
        FROM audit_logs al
        LEFT JOIN users u ON al.actor_user_id = u.id
        WHERE 1=1
        """
        args = []

        if entity_type and entity_type.upper() != "ALL":
            args.append(entity_type)
            clause = f" AND al.entity_type = ${len(args)}"
            query += clause
            count_query += clause

        if entity_id:
            args.append(str(entity_id))
            clause = f" AND al.entity_id = ${len(args)}"
            query += clause
            count_query += clause

        if action and action.upper() != "ALL":
            args.append(action)
            clause = f" AND al.action = ${len(args)}"
            query += clause
            count_query += clause

        if user_id and user_id.upper() != "ALL":
            args.append(user_id)
            clause = f" AND CAST(al.actor_user_id AS TEXT) = ${len(args)}"
            query += clause
            count_query += clause

        if date_from:
            args.append(date_from)
            clause = f" AND al.created_at >= ${len(args)}::timestamp"
            query += clause
            count_query += clause

        if date_to:
            args.append(date_to)
            clause = f" AND al.created_at <= ${len(args)}::timestamp"
            query += clause
            count_query += clause

        if search and search.strip():
            search_param = f"%{search.strip()}%"
            args.append(search_param)
            idx = len(args)
            clause = f""" AND (
                al.action ILIKE ${idx}
                OR al.entity_type ILIKE ${idx}
                OR al.entity_id ILIKE ${idx}
                OR u.full_name ILIKE ${idx}
                OR u.email ILIKE ${idx}
                OR ({entity_title_sql("al")}) ILIKE ${idx}
            )"""
            query += clause
            count_query += clause

        query += f" ORDER BY al.created_at DESC LIMIT {limit} OFFSET {offset}"

        records = await conn.fetch(query, *args)
        total = await conn.fetchval(count_query, *args)

        return [dict(r) for r in records], total or 0
