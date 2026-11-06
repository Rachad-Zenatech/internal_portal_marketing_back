"""The Paid Search board: load it, and save it whole (the board's order, its rows and their HubSpot links).

Saving replaces the saved board in one transaction: rows no longer on it are deleted and the rest are written in one
batch statement (no query per row). The last save wins, which is fine while one person edits the board at a time.
"""
import json
from typing import Any, Dict, List
from uuid import UUID

from models.paid_search.paid_search_model import FIELD_KEYS, PaidSearchEntry
from postgresql_db.database import get_pool

_EXTRA_KEYS = ("hubspot_campaign_id", "hubspot_link", "leads")
_COLUMNS = (*FIELD_KEYS, *_EXTRA_KEYS)
# The shape jsonb_to_recordset reads each saved row as
_RECORD_TYPES = ", ".join(["id uuid", "position int", *(f"{key} text" for key in _COLUMNS)])


async def list_paid_search_entries() -> List[Dict[str, Any]]:
    """Every saved entry, in board order."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id, {', '.join(_COLUMNS)} FROM paid_search_entries ORDER BY position, created_at"
        )
    entries = []
    for row in rows:
        entry = dict(row)
        entry["hubspot_link"] = entry["hubspot_link"].lower() if entry["hubspot_link"] else None
        entries.append(entry)
    return entries


async def save_paid_search_board(entries: List[PaidSearchEntry], user_id: UUID | None) -> int:
    """Replace the saved board with these entries, in this order. Returns how many were saved."""
    records = []
    for position, entry in enumerate(entries):
        record = entry.model_dump(mode="json")
        record["position"] = position
        # Stored upper case, like the other status codes
        record["hubspot_link"] = record["hubspot_link"].upper() if record["hubspot_link"] else None
        records.append(record)

    set_columns = ", ".join(f"{key} = EXCLUDED.{key}" for key in _COLUMNS)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM paid_search_entries WHERE NOT (id = ANY($1::uuid[]))",
                [entry.id for entry in entries],
            )
            if records:
                await conn.execute(
                    f"""
                    INSERT INTO paid_search_entries (id, position, {', '.join(_COLUMNS)}, updated_by, updated_at)
                    SELECT r.id, r.position, {', '.join(f'r.{key}' for key in _COLUMNS)}, $2, now()
                    FROM jsonb_to_recordset($1::jsonb) AS r({_RECORD_TYPES})
                    ON CONFLICT (id) DO UPDATE SET position = EXCLUDED.position, {set_columns},
                        updated_by = EXCLUDED.updated_by, updated_at = now()
                    """,
                    json.dumps(records),
                    user_id,
                )
    return len(records)
