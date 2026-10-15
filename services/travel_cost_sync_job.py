"""Pulls approved travel costs from the travel management system into each event's costs (spend_lines).

Runs as a background asyncio task started from server.py's lifespan when TRAVEL_COST_SYNC_ENABLED is on, every
TRAVEL_COST_SYNC_INTERVAL_MINUTES, in the same style as services/graph_sync_job.py. It can also be run on demand
from POST /api/integrations/travel/sync-costs.

How a run works:
- Only one run at a time: a second run in this process fails fast with TravelSyncAlreadyRunning, and so does a run
  that finds another server applying changes (non-blocking advisory lock).
- Reads the stored cursor with a short-lived connection, then asks the travel system for costs changed since the
  cursor (minus 5 minutes of overlap; everything on the first run) with no database connection held, so slow
  answers from the travel system can't use up the connection pool.
- Costs are matched to events by event_details.event_code. Costs for unknown codes, and costs that don't match the
  contract, are skipped and listed in the run summary; the cursor stops just before the earliest of them, so they
  are fetched again on the next run (applying a cost twice changes nothing).
- Each cost becomes one spend line with source TRAVEL_FEED and the travel id as external_ref, so re-runs update the
  same line. APPROVED becomes an Invoiced (actual) cost and PAID becomes Paid. CANCELLED or REJECTED removes the
  line, unless it is already reconciled: reconciled lines keep their figures and the change is counted as a conflict.
- The cost's own currency is kept (never converted). All writes, their audit entries and the new cursor are saved
  in one transaction, so the cursor only moves forward after a fully successful run.
- A one-event run (event_code given) applies that event's costs but leaves the stored sync state untouched.
"""

import asyncio
import datetime
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import TypeAdapter, ValidationError

from models.travel_integration_model import TravelCost
from postgresql_db.database import get_pool
from services.spend_service import _RECONCILED_FIELDS, _RECONCILED_PAID_FIELDS
from services.spend_service import AUDIT_ENTITY as SPEND_AUDIT_ENTITY
from services.travel_integration_service import TravelUnavailable, fetch_travel_costs

logger = logging.getLogger(__name__)

SYNC_NAME = "travel_costs"
SPEND_SOURCE = "TRAVEL_FEED"
# Re-read a little before the cursor, so a cost saved on the travel side while the last run was reading isn't missed
CURSOR_OVERLAP = datetime.timedelta(minutes=5)
DEFAULT_SYNC_INTERVAL_MINUTES = 360
MIN_SYNC_INTERVAL_MINUTES = 5

# Travel category -> our travel spend category (expense type TRAVEL_ACCOMMODATION, fed by the travel system)
CATEGORY_CODES = {
    "FLIGHT": "FLIGHTS",
    "LODGING": "HOTELS",
    "GROUND_TRANSPORT": "GROUND_TRANSPORT",
    "STIPEND": "PER_DIEMS_MEALS",
    "MEALS": "PER_DIEMS_MEALS",
    "CARGO": "OTHER_TRAVEL",
    "OTHER": "OTHER_TRAVEL",
}
# Used for the description when the travel system doesn't send one, e.g. "Flight · Air Canada"
CATEGORY_LABELS = {
    "FLIGHT": "Flight",
    "LODGING": "Hotel",
    "GROUND_TRANSPORT": "Ground transport",
    "STIPEND": "Stipend",
    "MEALS": "Meals",
    "CARGO": "Cargo",
    "OTHER": "Travel",
}
# Travel status -> our spend line status; the others remove the line
STATUS_MAP = {"APPROVED": "INVOICED", "PAID": "PAID"}

# Fields the sync keeps in step with the travel system (compared to decide whether a line changed)
_SYNCED_FIELDS = (
    "initiative_id", "category_id", "description", "vendor", "status", "amount", "currency", "incurred_on", "paid_on",
)
# On a reconciled line only these may still change (plus Invoiced -> Paid while not yet paid)
_RECONCILED_LOCKED = set(_RECONCILED_FIELDS) | {"initiative_id"}

_SYNC_LOCK_KEY = f"integration_sync_state:{SYNC_NAME}"
# At most this many skipped costs are listed in the run summary
MAX_SKIPPED_LISTED = 20
# The cursor stops this far before the earliest skipped cost, so the next run fetches it again
SKIPPED_CURSOR_MARGIN = datetime.timedelta(seconds=1)

ALREADY_RUNNING_MESSAGE = "A travel sync is already running. Try again in a minute."

# One run at a time in this process. Never waited on: a second run fails fast instead of queueing.
_run_lock = asyncio.Lock()
_datetime_adapter = TypeAdapter(datetime.datetime)


class TravelSyncAlreadyRunning(Exception):
    """Another travel cost sync is running (here or on another server). The message is safe to show to users."""

    def __init__(self, message: str = ALREADY_RUNNING_MESSAGE):
        super().__init__(message)


def travel_cost_sync_running() -> bool:
    """Whether a travel cost sync is running in this process."""
    return _run_lock.locked()


def travel_cost_sync_enabled() -> bool:
    return os.getenv("TRAVEL_COST_SYNC_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _sync_interval_seconds() -> int:
    try:
        minutes = int(os.getenv("TRAVEL_COST_SYNC_INTERVAL_MINUTES", str(DEFAULT_SYNC_INTERVAL_MINUTES)))
    except (TypeError, ValueError):
        minutes = DEFAULT_SYNC_INTERVAL_MINUTES
    return max(MIN_SYNC_INTERVAL_MINUTES, minutes) * 60


def _empty_summary() -> Dict[str, Any]:
    return {
        "fetched": 0, "created": 0, "updated": 0, "removed": 0,
        "skipped_unknown_event": 0, "conflicts": 0, "invalid": 0,
        # Up to MAX_SKIPPED_LISTED of the skipped costs: {"id": ..., "reason": ...}
        "skipped": [],
    }


class _Skipped:
    """Costs left out of this run, and how far the cursor may move so they're fetched again next run."""

    def __init__(self):
        self.items: List[Dict[str, Any]] = []
        self.earliest: Optional[datetime.datetime] = None
        # An invalid cost without a readable updated_at: the cursor can't safely move at all
        self.hold_cursor = False

    def add(self, cost_id: Optional[str], reason: str, updated_at: Optional[datetime.datetime]) -> None:
        self.items.append({"id": cost_id, "reason": reason})
        if updated_at is not None and updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=datetime.timezone.utc)
        if updated_at is None:
            self.hold_cursor = True
        elif self.earliest is None or updated_at < self.earliest:
            self.earliest = updated_at

    def next_cursor(self, server_time: datetime.datetime) -> Optional[datetime.datetime]:
        """The cursor to store after this run; None keeps the stored one."""
        if self.hold_cursor:
            return None
        if self.earliest is None:
            return server_time
        return min(server_time, self.earliest - SKIPPED_CURSOR_MARGIN)


def _parse_updated_at(item: Any) -> Optional[datetime.datetime]:
    """updated_at of a raw cost that failed validation, if it can be read."""
    if not isinstance(item, dict) or item.get("updated_at") in (None, ""):
        return None
    try:
        value = _datetime_adapter.validate_python(item["updated_at"])
    except ValidationError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.timezone.utc)


def _raw_id(item: Any) -> Optional[str]:
    if isinstance(item, dict) and item.get("id") is not None:
        return str(item["id"])[:100]
    return None


def _clip(value: Optional[str], length: int = 255) -> Optional[str]:
    return value[:length] if value else value


def _desired_line(cost: TravelCost, initiative_id, category_id: int) -> Optional[Dict[str, Any]]:
    """What the spend line should look like for this cost, or None when the cost means "no line"."""
    status = STATUS_MAP.get(cost.status)
    amount = cost.amount.quantize(Decimal("0.01"))
    if status is None or amount == 0:
        return None
    label = CATEGORY_LABELS[cost.category]
    description = cost.description or (f"{label} · {cost.vendor}" if cost.vendor else label)
    paid_on = None
    if status == "PAID":
        # A paid line needs a payment date; fall back to when the travel system last changed the cost
        paid_on = cost.paid_on or cost.updated_at.astimezone(datetime.timezone.utc).date()
    return {
        "initiative_id": initiative_id,
        "category_id": category_id,
        "description": _clip(description),
        "vendor": _clip(cost.vendor),
        "status": status,
        "amount": amount,
        "currency": cost.currency,
        "incurred_on": cost.incurred_on,
        "paid_on": paid_on,
    }


def _reconciled_change_allowed(current, changed: Dict[str, Any]) -> bool:
    """Whether these changes may be applied to a reconciled line (same rules as people editing it)."""
    locked = set(_RECONCILED_LOCKED)
    if current["status"] == "PAID":
        locked |= set(_RECONCILED_PAID_FIELDS)
    if any(field in locked for field in changed):
        return False
    if "status" in changed and (current["status"], changed["status"]) != ("INVOICED", "PAID"):
        return False
    return True


def _audit_values(values: Dict[str, Any], external_ref: str) -> Dict[str, Any]:
    return {**values, "source": SPEND_SOURCE, "external_ref": external_ref}


def _latest_by_id(costs: List[TravelCost]) -> List[TravelCost]:
    """One cost per travel id: the most recently updated, should the travel system repeat one."""
    latest: Dict[str, TravelCost] = {}
    for cost in costs:
        kept = latest.get(cost.id)
        if kept is None or cost.updated_at >= kept.updated_at:
            latest[cost.id] = cost
    return list(latest.values())


async def _apply_costs(conn, costs: List[TravelCost], summary: Dict[str, Any], skipped: _Skipped) -> None:
    """Create, update and remove TRAVEL_FEED lines for these costs, with audit entries. Batch statements only."""
    if not costs:
        return

    codes = sorted({c.event_code for c in costs})
    events = {
        r["event_code"]: r["initiative_id"]
        for r in await conn.fetch(
            "SELECT event_code, initiative_id FROM event_details WHERE event_code = ANY($1::text[])", codes
        )
    }
    categories = {
        r["code"]: r
        for r in await conn.fetch(
            "SELECT id, code, default_cost_type FROM spend_categories WHERE code = ANY($1::text[])",
            sorted(set(CATEGORY_CODES.values())),
        )
    }
    missing = set(CATEGORY_CODES.values()) - set(categories)
    if missing:
        raise RuntimeError(f"Travel spend categories are missing: {', '.join(sorted(missing))}")

    existing = {
        r["external_ref"]: r
        for r in await conn.fetch(
            """
            SELECT id, external_ref, initiative_id, category_id, description, vendor, status, amount, currency,
                   incurred_on, paid_on, reconciled_month
            FROM spend_lines
            WHERE source = $1 AND external_ref = ANY($2::text[])
            FOR UPDATE
            """,
            SPEND_SOURCE, [c.id for c in costs],
        )
    }

    creates: List[Dict[str, Any]] = []
    updates: List[Dict[str, Any]] = []
    deletes: List[Any] = []
    # (action, line id or None for creates, old value, new value)
    audits: List[tuple] = []

    for cost in costs:
        initiative_id = events.get(cost.event_code)
        if initiative_id is None:
            summary["skipped_unknown_event"] += 1
            reason = f"unknown event code {cost.event_code}"
            skipped.add(cost.id, reason, cost.updated_at)
            logger.warning(
                f"Skipping travel cost {cost.id}: {reason}",
                extra={"event": "travel_cost_unknown_event", "cost_id": cost.id, "event_code": cost.event_code},
            )
            continue
        category = categories[CATEGORY_CODES[cost.category]]
        desired = _desired_line(cost, initiative_id, category["id"])
        current = existing.get(cost.id)

        if desired is None:
            if current is None:
                continue
            if current["reconciled_month"] is not None:
                summary["conflicts"] += 1
                continue
            deletes.append(current["id"])
            old = {f: current[f] for f in _SYNCED_FIELDS}
            audits.append(("DELETE", current["id"], _audit_values(old, cost.id), None))
            summary["removed"] += 1
            continue

        if current is None:
            creates.append({**desired, "cost_type": category["default_cost_type"], "external_ref": cost.id})
            continue

        changed = {f: v for f, v in desired.items() if current[f] != v}
        if not changed:
            continue
        if current["reconciled_month"] is not None and not _reconciled_change_allowed(current, changed):
            summary["conflicts"] += 1
            continue
        # Keep every synced field in the update so one batch statement covers all changed lines
        updates.append({**desired, "id": current["id"]})
        old = {"initiative_id": current["initiative_id"], **{f: current[f] for f in changed}}
        new = {"initiative_id": initiative_id, **changed}
        audits.append(("UPDATE", current["id"], _audit_values(old, cost.id), _audit_values(new, cost.id)))
        summary["updated"] += 1

    if creates:
        created = await conn.fetch(
            """
            INSERT INTO spend_lines (initiative_id, category_id, description, vendor, status, amount, currency,
                                     incurred_on, paid_on, cost_type, source, external_ref)
            SELECT u.initiative_id, u.category_id, u.description, u.vendor, u.status, u.amount, u.currency,
                   u.incurred_on, u.paid_on, u.cost_type, $11, u.external_ref
            FROM unnest($1::uuid[], $2::int[], $3::text[], $4::text[], $5::text[], $6::numeric[], $7::text[],
                        $8::date[], $9::date[], $10::text[], $12::text[])
                 AS u(initiative_id, category_id, description, vendor, status, amount, currency,
                      incurred_on, paid_on, cost_type, external_ref)
            RETURNING id, external_ref
            """,
            [c["initiative_id"] for c in creates], [c["category_id"] for c in creates],
            [c["description"] for c in creates], [c["vendor"] for c in creates], [c["status"] for c in creates],
            [c["amount"] for c in creates], [c["currency"] for c in creates], [c["incurred_on"] for c in creates],
            [c["paid_on"] for c in creates], [c["cost_type"] for c in creates], SPEND_SOURCE,
            [c["external_ref"] for c in creates],
        )
        ids = {r["external_ref"]: r["id"] for r in created}
        for c in creates:
            values = {k: v for k, v in c.items() if k != "external_ref"}
            audits.append(("CREATE", ids[c["external_ref"]], None, _audit_values(values, c["external_ref"])))
        summary["created"] += len(creates)

    if updates:
        await conn.execute(
            """
            UPDATE spend_lines s
            SET initiative_id = u.initiative_id, category_id = u.category_id, description = u.description,
                vendor = u.vendor, status = u.status, amount = u.amount, currency = u.currency,
                incurred_on = u.incurred_on, paid_on = u.paid_on, updated_at = now(), updated_by = NULL
            FROM unnest($1::uuid[], $2::uuid[], $3::int[], $4::text[], $5::text[], $6::text[], $7::numeric[],
                        $8::text[], $9::date[], $10::date[])
                 AS u(id, initiative_id, category_id, description, vendor, status, amount, currency, incurred_on, paid_on)
            WHERE s.id = u.id
            """,
            [u["id"] for u in updates], [u["initiative_id"] for u in updates], [u["category_id"] for u in updates],
            [u["description"] for u in updates], [u["vendor"] for u in updates], [u["status"] for u in updates],
            [u["amount"] for u in updates], [u["currency"] for u in updates], [u["incurred_on"] for u in updates],
            [u["paid_on"] for u in updates],
        )

    if deletes:
        await conn.execute("DELETE FROM spend_lines WHERE id = ANY($1::uuid[])", deletes)

    if audits:
        # Same columns log_audit writes, as one statement. The actor is empty: the travel system made the change.
        await conn.execute(
            """
            INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, old_value, new_value, created_at)
            SELECT NULL, a.action, $1, a.entity_id, CAST(a.old_value AS jsonb), CAST(a.new_value AS jsonb), now()
            FROM unnest($2::text[], $3::text[], $4::text[], $5::text[]) AS a(action, entity_id, old_value, new_value)
            """,
            SPEND_AUDIT_ENTITY,
            [a[0] for a in audits],
            [str(a[1]) for a in audits],
            [json.dumps(a[2], default=str) if a[2] is not None else None for a in audits],
            [json.dumps(a[3], default=str) if a[3] is not None else None for a in audits],
        )


async def _record_failure(message: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO integration_sync_state (name, last_attempt_at, last_error)
            VALUES ($1, now(), $2)
            ON CONFLICT (name) DO UPDATE SET last_attempt_at = now(), last_error = EXCLUDED.last_error
            """,
            SYNC_NAME, message,
        )


def _validate_costs(raw_costs: List[Any], summary: Dict[str, Any], skipped: _Skipped) -> List[TravelCost]:
    costs: List[TravelCost] = []
    for item in raw_costs:
        try:
            costs.append(TravelCost.model_validate(item))
        except ValidationError as exc:
            summary["invalid"] += 1
            cost_id = _raw_id(item)
            errors = exc.errors(include_url=False, include_context=False, include_input=False)[:3]
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'cost'}: {e['msg']}" for e in errors)
            reason = f"does not match the contract ({problems})"
            skipped.add(cost_id, reason, _parse_updated_at(item))
            logger.warning(
                f"Skipping travel cost {cost_id or '(no id)'}: {reason}",
                extra={"event": "travel_cost_invalid", "cost_id": cost_id, "errors": errors},
            )
    return costs


async def _run_sync(event_code: Optional[str]) -> Dict[str, Any]:
    summary = _empty_summary()
    skipped = _Skipped()
    pool = get_pool()

    # 1. Where the last run stopped, on a short-lived connection
    cursor = None
    if not event_code:
        async with pool.acquire() as conn:
            cursor = await conn.fetchval("SELECT cursor FROM integration_sync_state WHERE name = $1", SYNC_NAME)
    updated_since = None if cursor is None else cursor - CURSOR_OVERLAP

    # 2. Ask the travel system with no database connection held (it can take up to its timeout)
    response = await fetch_travel_costs(updated_since, event_code=event_code)
    summary["fetched"] = len(response.costs)
    costs = _validate_costs(response.costs, summary, skipped)
    if event_code:
        costs = [c for c in costs if c.event_code == event_code]

    # 3. Apply everything in one transaction, unless another server is applying right now
    async with pool.acquire() as conn:
        async with conn.transaction():
            locked = await conn.fetchval("SELECT pg_try_advisory_xact_lock(hashtext($1))", _SYNC_LOCK_KEY)
            if not locked:
                raise TravelSyncAlreadyRunning()

            await _apply_costs(conn, _latest_by_id(costs), summary, skipped)
            summary["skipped"] = skipped.items[:MAX_SKIPPED_LISTED]

            if not event_code:
                await conn.execute(
                    """
                    INSERT INTO integration_sync_state (name, cursor, last_attempt_at, last_success_at, last_error,
                                                        last_run_summary)
                    VALUES ($1, $2, now(), now(), NULL, CAST($3 AS jsonb))
                    ON CONFLICT (name) DO UPDATE SET
                        cursor = COALESCE(EXCLUDED.cursor, integration_sync_state.cursor),
                        last_attempt_at = now(), last_success_at = now(), last_error = NULL,
                        last_run_summary = EXCLUDED.last_run_summary
                    """,
                    SYNC_NAME, skipped.next_cursor(response.server_time), json.dumps(summary),
                )
    return summary


async def sync_travel_costs(event_code: Optional[str] = None) -> Dict[str, Any]:
    """Run one sync and return its summary.

    With event_code, only that event's costs are fetched (all of them, not just recent changes) and applied; the
    stored sync state (cursor, last success, last error, last summary) is left untouched, since the run didn't see
    every event's changes.

    Raises TravelSyncAlreadyRunning when another run is in progress (nothing is stored), and TravelUnavailable when
    the travel system isn't configured or can't be reached (the error is stored for full runs).
    """
    event_code = event_code.strip().upper() if event_code else None
    if _run_lock.locked():
        raise TravelSyncAlreadyRunning()

    async with _run_lock:
        try:
            summary = await _run_sync(event_code)
        except TravelSyncAlreadyRunning:
            logger.info(
                "Travel cost sync skipped: another server is applying travel costs",
                extra={"event": "travel_cost_sync_already_running", "event_code": event_code},
            )
            raise
        except TravelUnavailable as exc:
            logger.warning(
                f"Travel cost sync could not reach the travel system: {exc}",
                extra={"event": "travel_cost_sync_unavailable", "error": str(exc), "event_code": event_code},
            )
            if not event_code:
                await _record_failure(str(exc))
            raise
        except Exception as exc:
            logger.exception(
                f"Travel cost sync failed: {type(exc).__name__}: {exc}",
                extra={"event": "travel_cost_sync_failed", "error": f"{type(exc).__name__}: {exc}",
                       "event_code": event_code},
            )
            if not event_code:
                await _record_failure(
                    f"The sync stopped with an unexpected error ({type(exc).__name__}). Nothing was changed."
                )
            raise

    logger.info(
        "Travel cost sync completed",
        extra={"event": "travel_cost_sync_completed", "event_code": event_code, "summary": summary},
    )
    return summary


async def get_travel_sync_state() -> Optional[Dict[str, Any]]:
    """The stored state of the travel cost sync, or None before its first run."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT cursor, last_attempt_at, last_success_at, last_error, last_run_summary
            FROM integration_sync_state WHERE name = $1
            """,
            SYNC_NAME,
        )
    if row is None:
        return None
    state = dict(row)
    if isinstance(state["last_run_summary"], str):
        state["last_run_summary"] = json.loads(state["last_run_summary"])
    return state


async def travel_cost_sync_loop(interval_seconds: Optional[int] = None) -> None:
    """Run sync_travel_costs() forever on a fixed interval. A failed run is logged and retried next interval
    rather than ending the task, matching graph_sync_loop."""
    interval = interval_seconds or _sync_interval_seconds()
    while True:
        try:
            await sync_travel_costs()
        except TravelSyncAlreadyRunning:
            logger.info(
                "Scheduled travel cost sync skipped: another sync is already running",
                extra={"event": "travel_cost_sync_run_skipped"},
            )
        except TravelUnavailable:
            # Already logged and stored by sync_travel_costs
            pass
        except Exception:
            logger.exception(
                "Travel cost sync iteration crashed; will retry next interval",
                extra={"event": "travel_cost_sync_iteration_crashed"},
            )
        await asyncio.sleep(interval)
