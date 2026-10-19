"""An initiative's recent activity in plain English, read from the audit log.

Covers the initiative's own entries plus its costs, results, uploads, links (recorded on the initiative's entries),
evaluation settings, decisions, ad results, demos and staff.
"""
import json
from datetime import date
from uuid import UUID

from postgresql_db.database import get_pool
from services.audit_service import entity_title_sql
from services.initiative_service import InitiativeNotFoundError

# Sources that change things without a person: the name shown as the actor
SOURCE_NAMES = {
    "NAVAN": "Navan",
    "HUBSPOT": "HubSpot",
    "GOOGLE_ADS": "Google Ads",
    "MICROSOFT_ADS": "Microsoft Ads",
    "LINKEDIN_ADS": "LinkedIn Ads",
    "META_ADS": "Meta Ads",
    "DATA_STUDIO": "Data Studio",
}
_LINK_SYSTEMS = {k: v for k, v in SOURCE_NAMES.items() if k in {"HUBSPOT", "GOOGLE_ADS", "MICROSOFT_ADS", "LINKEDIN_ADS", "META_ADS"}}

_STATUS_LABELS = {
    "DRAFT": "Draft", "PLANNED": "Planned", "ACTIVE": "Active", "COMPLETED": "Completed", "PAUSED": "Paused",
    "CANCELLED": "Cancelled",
}
_COST_STATUS_LABELS = {"ESTIMATED": "estimated", "COMMITTED": "committed", "INVOICED": "invoiced", "PAID": "paid"}
_FIELD_NAMES = {
    "name": "name", "brand_id": "brand", "channel_id": "channel", "sub_channel_id": "sub-channel",
    "funnel_stage_id": "funnel stage", "goal_id": "goal", "owner_user_id": "owner", "start_date": "start date",
    "end_date": "end date", "budget_amount": "budget", "currency": "currency", "status": "status", "market_id": "market",
    "description": "description", "primary_kpi_id": "primary KPI", "kpi_target": "KPI target",
    "data_source": "data source", "channel_details": "channel details", "event": "event details",
    "links": "campaign links",
}
# Entries recorded against the initiative's own id, and entries that name it in their values
_OWN_TYPES = ["Initiative", "Outcomes", "Evaluation"]
_RELATED_TYPES = ["SpendLine", "ImportBatch", "Decision", "AdResult", "Demo", "EventStaff"]

_ACTIVITY_SQL = f"""
    SELECT a.created_at AS at, a.action, a.entity_type, a.old_value, a.new_value, a.actor_user_id,
           COALESCE(u.full_name, u.email) AS user_name,
           {entity_title_sql("a")} AS entity_title,
           sc.name AS category_name,
           COALESCE(a.new_value->>'source', a.old_value->>'source', line.source) AS source,
           i.currency, line.currency AS line_currency
    FROM audit_logs a
    JOIN initiatives i ON i.id = $1
    LEFT JOIN users u ON u.id = a.actor_user_id
    -- Costs: the line as it is now (Navan's updates don't record the initiative or category)
    LEFT JOIN spend_lines line ON a.entity_type = 'SpendLine' AND CAST(line.id AS TEXT) = a.entity_id
    LEFT JOIN spend_categories sc
           ON a.entity_type = 'SpendLine'
          AND sc.id = COALESCE(CAST(COALESCE(a.new_value->>'category_id', a.old_value->>'category_id') AS INT), line.category_id)
    WHERE (a.entity_type = ANY($2::text[]) AND a.entity_id = $3)
       OR (a.entity_type = ANY($4::text[])
           AND (a.new_value->>'initiative_id' = $3 OR a.old_value->>'initiative_id' = $3 OR line.initiative_id = $1))
    ORDER BY a.created_at DESC, a.id DESC
    LIMIT $5
"""


def _json(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def _number(value) -> str:
    number = float(value)
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}"


def _money(value, currency: str | None) -> str:
    text = f"{float(value):,.2f}"
    return f"{text} {currency}" if currency else text


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


def _month(value) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%b %Y")
    except ValueError:
        return str(value)


def _link_text(link: dict) -> str:
    return f"{_LINK_SYSTEMS.get(link.get('system'), link.get('system'))} campaign {link.get('external_id')}"


def _initiative_entry(action: str, old: dict, new: dict, currency: str, source: str | None) -> tuple[str, str | None]:
    if action == "CREATE":
        if source == "NAVAN":
            return "Initiative created", "Created from a Navan event"
        if new.get("status") == "DRAFT":
            return "Initiative created", "Saved as a draft"
        return "Initiative created", None
    if action == "DELETE":
        return "Initiative deleted", None

    fields = [f for f in new if f != "source"]
    if "status" in fields:
        others = [_FIELD_NAMES.get(f, f) for f in fields if f != "status"]
        return f"Status changed to {_STATUS_LABELS.get(new['status'], new['status'])}", (
            f"Also changed: {_join(others)}" if others else None
        )
    if fields == ["budget_amount"]:
        before, after = old.get("budget_amount"), new.get("budget_amount")
        if before is None:
            return "Budget updated", f"Budget set to {_money(after, currency)}" if after is not None else None
        if after is None:
            return "Budget updated", "Budget removed"
        return "Budget updated", f"Budget changed from {_money(before, currency)} to {_money(after, currency)}"
    if fields == ["links"]:
        before = {(link["system"], link["external_id"]): link for link in old.get("links") or []}
        after = {(link["system"], link["external_id"]): link for link in new.get("links") or []}
        parts = [f"Linked {_link_text(link)}" for key, link in after.items() if key not in before]
        parts += [f"Removed {_link_text(link)}" for key, link in before.items() if key not in after]
        return "Campaign links updated", "; ".join(parts) or "Account IDs changed"
    if fields == ["event"] and set((new.get("event") or {})) <= {"navan_estimate_amount", "event_code"}:
        amount = (new.get("event") or {}).get("navan_estimate_amount")
        return "Navan estimate updated", f"Navan estimate set to {_money(amount, currency)}" if amount is not None else "Navan estimate removed"
    return "Details updated", f"Changed {_join([_FIELD_NAMES.get(f, f) for f in fields])}" if fields else None


def _cost_entry(
    action: str, old: dict, new: dict, category: str | None, source: str | None, line_currency: str | None
) -> tuple[str, str | None]:
    what = f"{category} cost" if category else "Cost"
    from_source = f" from {SOURCE_NAMES[source]}" if source in SOURCE_NAMES else ""
    if action == "CREATE":
        amount = new.get("amount")
        detail = f"{what} added{from_source}"
        return "Expense added", f"{detail}: {_money(amount, new.get('currency'))}" if amount is not None else detail
    if action == "DELETE":
        amount = old.get("amount")
        detail = f"{what} removed{from_source}"
        return "Expense removed", f"{detail}: {_money(amount, old.get('currency'))}" if amount is not None else detail
    if "amount" in new and "amount" in old:
        currency = new.get("currency") or old.get("currency") or line_currency
        return "Expense updated", f"{what} changed from {_money(old['amount'], currency)} to {_money(new['amount'], currency)}{from_source}"
    if "status" in new:
        return "Expense updated", f"{what} marked {_COST_STATUS_LABELS.get(new['status'], new['status'])}"
    return "Expense updated", f"{what} details changed{from_source}"


def _results_entry(old: dict, new: dict, metric_names: dict, source: str | None) -> tuple[str, str | None]:
    month = new.get("period_start") or old.get("period_start")
    values = {k: v for k, v in new.items() if k not in ("period_start", "source")}
    set_parts = [f"{_number(v)} {metric_names.get(k, k).lower()}" for k, v in values.items() if v is not None]
    removed = [metric_names.get(k, k) for k, v in values.items() if v is None]
    parts = []
    if set_parts:
        verb = f"imported from {SOURCE_NAMES[source]}" if source in SOURCE_NAMES else "recorded"
        parts.append(f"{_join(set_parts)} {verb}")
    if removed:
        parts.append(f"{_join(removed)} removed")
    detail = "; ".join(parts) or None
    if detail and month:
        detail = f"{detail} for {_month(month)}"
    title = "Lead data synced" if source == "HUBSPOT" else "Results updated"
    return title, detail


_GENERIC_TITLES = {
    "ImportBatch": {"IMPORT": "Sheet uploaded", "UNDO_IMPORT": "Upload undone"},
    "Decision": {"CREATE": "Decision recorded"},
    "AdResult": {"CREATE": "Ad results added", "UPDATE": "Ad results updated", "DELETE": "Ad results removed"},
    "Demo": {"CREATE": "Demo logged", "UPDATE": "Demo updated", "DELETE": "Demo removed"},
    "EventStaff": {"CREATE": "Staff member added", "UPDATE": "Staff member updated", "DELETE": "Staff member removed"},
}


def _describe(row, metric_names: dict) -> tuple[str, str | None]:
    old, new = _json(row["old_value"]), _json(row["new_value"])
    kind, action, source = row["entity_type"], row["action"], row["source"]
    if kind == "Initiative":
        return _initiative_entry(action, old, new, row["currency"], source)
    if kind == "SpendLine":
        return _cost_entry(action, old, new, row["category_name"], source, row["line_currency"])
    if kind == "Outcomes":
        return _results_entry(old, new, metric_names, source)
    if kind == "Evaluation":
        days = new.get("maturity_days")
        return "Evaluation settings updated", f"Maturity window set to {days} days" if days is not None else None
    if kind == "ImportBatch":
        values = new if action == "IMPORT" else old
        filename = values.get("filename") or "Sheet"
        if action == "IMPORT":
            rows = values.get("imported_rows")
            return "Sheet uploaded", f"{filename}: {rows} rows imported" if rows is not None else filename
        rows = values.get("removed_rows")
        return "Upload undone", f"{filename}: {rows} rows removed" if rows is not None else filename
    title = _GENERIC_TITLES.get(kind, {}).get(action, f"{kind} {action.lower()}")
    return title, row["entity_title"]


def _actor(row) -> str | None:
    if row["actor_user_id"] is not None:
        # None when the person has since been removed
        return row["user_name"]
    return SOURCE_NAMES.get(row["source"], "System")


async def list_activity(initiative_id: UUID, limit: int = 10) -> list[dict]:
    """Newest first: {at, actor_name, title, detail}."""
    pool = get_pool()
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM initiatives WHERE id = $1)", initiative_id):
            raise InitiativeNotFoundError
        rows = await conn.fetch(_ACTIVITY_SQL, initiative_id, _OWN_TYPES, str(initiative_id), _RELATED_TYPES, limit)
        metric_names = {r["code"]: r["name"] for r in await conn.fetch("SELECT code, name FROM outcome_metrics")}

    items = []
    for row in rows:
        title, detail = _describe(row, metric_names)
        items.append({"at": row["at"], "actor_name": _actor(row), "title": title, "detail": detail})
    return items
