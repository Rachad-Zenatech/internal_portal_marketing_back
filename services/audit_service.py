import json
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from postgresql_db.database import get_pool


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
