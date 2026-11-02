"""Set many events' budgets from one spreadsheet: preview what would change, then apply it.

The sheet has one row per event: the event (its code, e.g. EVT-2026-0012, or its exact name), its budget, and
optionally the budget's currency and the event's travel budget. Nothing is saved by the preview; apply re-checks every
row against the events as they are now, then updates them in one transaction with an audit entry each (shown in the
event's activity as "Budget changed from … to …").
"""
from decimal import Decimal
from typing import Any
from uuid import UUID

from postgresql_db.database import get_pool
from services.shared.ads_service import audit_many
from services.shared.import_parsing import (
    ROW_NUMBER,
    ImportFileError,
    InvalidValueError,
    match_columns,
    parse_amount,
    parse_currency,
    read_table,
    uses_decimal_comma,
)

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_AMOUNT = Decimal("999999999999")
AUDIT_ENTITY = "Initiative"

COLUMNS = {
    "event": ["event code", "code", "event name", "event", "name", "initiative", "initiative name"],
    "budget": ["event budget", "allocated budget", "approved budget", "budget amount", "amount"],
    "currency": ["budget currency"],
    "travel_budget": ["travel budget", "travel"],
}
KNOWN_HEADERS = [name for field, names in COLUMNS.items() for name in [field, *names]]
REQUIRED_LABELS = {"event": "Event (its code or name)", "budget": "Budget"}

_EVENTS_SQL = """
    SELECT i.id, i.name, i.status, i.currency, i.budget_amount::float8 AS budget_amount,
           ed.event_code, ed.travel_budget_amount::float8 AS travel_budget_amount
    FROM initiatives i
    JOIN channels c ON c.id = i.channel_id AND c.code = 'EVENTS'
    LEFT JOIN event_details ed ON ed.initiative_id = i.id
"""


class EventBudgetImportError(Exception):
    """A row that can't be applied (apply re-checks everything the preview checked)."""


def _number(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _same(a: float | None, b: float | None) -> bool:
    return (a is None and b is None) or (a is not None and b is not None and abs(a - b) < 0.005)


def _check(budget: float | None, travel: float | None) -> str | None:
    """Why these values can't be saved on this event, or None"""
    if budget is None:
        return "No budget. Enter 0 if the event doesn't have one."
    if budget < 0 or travel is not None and travel < 0:
        return "Budgets can't be negative."
    if budget > MAX_AMOUNT or travel is not None and travel > MAX_AMOUNT:
        return "The amount is too large."
    if travel is not None and travel >= budget:
        return "The travel budget has to be smaller than the event budget."
    return None


def _find(events_by_code: dict, events_by_name: dict, text: str) -> tuple[dict | None, str | None]:
    key = text.strip().lower()
    if not key:
        return None, "No event. Add the event code or its name."
    if key in events_by_code:
        return events_by_code[key], None
    matches = events_by_name.get(key, [])
    if len(matches) > 1:
        return None, f"Several events are called '{text.strip()}'. Use the event code instead."
    if matches:
        return matches[0], None
    return None, f"No event with the code or name '{text.strip()}'."


async def preview(filename: str, data: bytes) -> dict[str, Any]:
    """What uploading this sheet would change, row by row. Saves nothing."""
    try:
        table = read_table(filename, data, KNOWN_HEADERS)
    except ImportFileError as exc:
        if "header row" not in str(exc):
            raise
        raise ImportFileError(f"The sheet needs these columns: {', '.join(REQUIRED_LABELS.values())}.") from exc
    headers = [h for h in table[0] if h != ROW_NUMBER] if table else []
    columns = match_columns(headers, COLUMNS)
    missing = [label for field, label in REQUIRED_LABELS.items() if field not in columns.fields]
    if missing:
        raise ImportFileError(f"The sheet needs these columns: {', '.join(missing)}.")

    pool = get_pool()
    async with pool.acquire() as conn:
        events = [dict(r) for r in await conn.fetch(_EVENTS_SQL)]
    by_code = {e["event_code"].lower(): e for e in events if e["event_code"]}
    by_name: dict[str, list[dict]] = {}
    for e in events:
        by_name.setdefault(e["name"].strip().lower(), []).append(e)

    def cell(record: dict, field: str) -> str:
        header = columns.fields.get(field)
        return str(record.get(header, "") or "") if header else ""

    decimal_comma = uses_decimal_comma(cell(r, f) for r in table for f in ("budget", "travel_budget"))
    rows: list[dict[str, Any]] = []
    seen: dict[UUID, int] = {}
    for record in table:
        event_text = cell(record, "event")
        row: dict[str, Any] = {"row_number": record[ROW_NUMBER], "event_text": event_text.strip(), "status": "ERROR", "message": None,
                               "initiative_id": None, "event_name": None, "event_code": None, "event_status": None,
                               "old_budget": None, "new_budget": None, "old_currency": None, "new_currency": None,
                               "old_travel_budget": None, "new_travel_budget": None}
        rows.append(row)
        event, problem = _find(by_code, by_name, event_text)
        if event is None:
            row["message"] = problem
            continue
        row.update(initiative_id=event["id"], event_name=event["name"], event_code=event["event_code"], event_status=event["status"],
                   old_budget=event["budget_amount"], old_currency=event["currency"], old_travel_budget=event["travel_budget_amount"])
        if event["id"] in seen:
            row["message"] = f"This event is already in row {seen[event['id']]}."
            continue
        seen[event["id"]] = record[ROW_NUMBER]
        try:
            budget = _number(parse_amount(cell(record, "budget"), decimal_comma))
            currency = parse_currency(cell(record, "currency"), event["currency"]) if "currency" in columns.fields else event["currency"]
            # A blank travel budget cell (or no column) keeps the event's travel budget
            travel_text = cell(record, "travel_budget")
            travel = _number(parse_amount(travel_text, decimal_comma)) if travel_text.strip() else event["travel_budget_amount"]
        except InvalidValueError as exc:
            row["message"] = str(exc)
            continue
        row.update(new_budget=budget, new_currency=currency, new_travel_budget=travel)
        problem = _check(budget, travel)
        if problem:
            row["message"] = problem
        elif _same(budget, event["budget_amount"]) and currency == event["currency"] and _same(travel, event["travel_budget_amount"]):
            row["status"] = "UNCHANGED"
        else:
            row["status"] = "UPDATE"

    counts = {s: sum(r["status"] == s for r in rows) for s in ("UPDATE", "UNCHANGED", "ERROR")}
    return {"rows": rows, "counts": counts, "ignored_columns": columns.ignored}


async def apply(changes: list[dict[str, Any]], actor_id: UUID) -> dict[str, int]:
    """Save the budgets ({initiative_id, budget_amount, currency, travel_budget_amount} each). All or nothing."""
    if not changes:
        return {"updated": 0}
    ids = [c["initiative_id"] for c in changes]
    if len(set(ids)) != len(ids):
        raise EventBudgetImportError("An event appears more than once.")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = {r["id"]: dict(r) for r in await conn.fetch(f"{_EVENTS_SQL} WHERE i.id = ANY($1::uuid[]) FOR UPDATE OF i", ids)}
            updates, audits = [], []
            for change in changes:
                event = current.get(change["initiative_id"])
                if event is None:
                    raise EventBudgetImportError("One of the events no longer exists, or isn't an event. Upload the sheet again.")
                budget, currency, travel = change["budget_amount"], change["currency"], change["travel_budget_amount"]
                problem = _check(budget, travel)
                if problem:
                    raise EventBudgetImportError(f"{event['name']}: {problem}")
                old, new = {}, {}
                for field, before, after in (("budget_amount", event["budget_amount"], budget), ("currency", event["currency"], currency)):
                    if not (before == after if field == "currency" else _same(before, after)):
                        old[field], new[field] = before, after
                if not _same(event["travel_budget_amount"], travel):
                    old["event"] = {"travel_budget_amount": event["travel_budget_amount"]}
                    new["event"] = {"travel_budget_amount": travel, "event_code": event["event_code"]}
                if new:
                    updates.append((event["id"], budget, currency, travel))
                    audits.append(("UPDATE", event["id"], old, {**new, "source": "BUDGET_UPLOAD"}))

            if updates:
                await conn.execute(
                    """
                    UPDATE initiatives i
                    SET budget_amount = u.budget, currency = u.currency, updated_at = now(), updated_by = $4
                    FROM unnest($1::uuid[], $2::numeric[], $3::text[]) AS u(id, budget, currency)
                    WHERE i.id = u.id
                    """,
                    [u[0] for u in updates], [u[1] for u in updates], [u[2] for u in updates], actor_id,
                )
                await conn.execute(
                    """
                    UPDATE event_details ed
                    SET travel_budget_amount = u.travel, updated_at = now(), updated_by = $3
                    FROM unnest($1::uuid[], $2::numeric[]) AS u(id, travel)
                    WHERE ed.initiative_id = u.id AND ed.travel_budget_amount IS DISTINCT FROM u.travel
                    """,
                    [u[0] for u in updates], [u[3] for u in updates], actor_id,
                )
                await audit_many(conn, actor_id, AUDIT_ENTITY, audits)
    return {"updated": len(updates)}
