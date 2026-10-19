"""Keeps each event's Navan travel cost up to date: one cost line per event, booking type and currency with the
total of the event's active Navan bookings of that type.

Runs as a background asyncio task started from server.py's lifespan when NAVAN_COST_SYNC_ENABLED is on, every
NAVAN_COST_SYNC_INTERVAL_MINUTES, like services/graph_sync_job.py. It can also be run on demand from
POST /api/integrations/navan/sync-costs.

How a run works:
- Reads every booking in the lookback window from Navan with no database connection held, and adds up the active
  ones per Navan event (services/navan_service.summarize_events).
- Links Navan events to our events: an event whose "Navan event" (event_details.navan_event_name) is set uses that
  name; otherwise an event whose own name is the same as a Navan event's (ignoring case) is used, when exactly one
  event has that name and no other event has claimed it.
- A Navan event with active bookings and no event here gets one when NAVAN_CREATE_EVENTS is on (off by default):
  named as in Navan and linked to it, brand TBD/All, In-person, in the bookings' currency, from the first to the
  last day of travel, in the city where most of the hotel nights are, Planned / Active / Completed by its dates.
  Nobody is recorded as creating it. A Navan event with no bookings can't be seen (Navan's API has no list of events).
- Each linked event gets one spend line per booking type and currency: source NAVAN, external_ref
  "navan:<initiative id>:<booking type>:<currency>", status Invoiced (Navan charges when booking), dated on the
  first day of travel, described like "Navan flights (3 bookings)". The category follows the booking type
  (BOOKING_TYPE_CATEGORIES): flights, hotels, cars as ground transport, rail and anything else as other travel, so
  the Events Overview cost breakdown splits them. A line whose Navan event was read but has no active bookings of
  that type left, or whose event is no longer linked to it, is removed. A Navan event missing from the run (all
  its bookings are older than the lookback window) keeps its lines as they are. Reconciled lines keep their
  figures and the change is counted as a conflict.
- Lines from before the split ("navan:<initiative id>:<currency>", one per event and currency) are replaced by the
  per-type lines in the first run that reads the event's bookings. When such a line is reconciled it stays (a
  conflict) and no per-type lines are added for that event and currency, so nothing is counted twice.
- When the source gives an estimate for a linked event (only demo mode does: Navan's API has no event estimates)
  and it's in the event's currency, the event's Navan estimate (event_details.navan_estimate_amount) is set to it.
- NAVAN_MODE=demo reads generated bookings for the demo events (services/navan_demo_service.py) instead of Navan,
  through the same steps, and only looks at demo events and their lines: real events and their Navan costs are
  left alone. Its run state is kept apart from the live one (integration_sync_state "navan_costs_demo").
- Travel never counts against a marketing budget (services/budget_service), only toward the event's cost and ROI.
- Every write, its audit entry and the run summary are saved in one transaction.
"""

import asyncio
import datetime
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

from postgresql_db.database import get_pool
from services.initiative_service import AUDIT_ENTITY as INITIATIVE_AUDIT_ENTITY
from services.navan_demo_service import DEMO_EVENT_PREFIX, fetch_demo_navan_data
from services.navan_service import (
    DEMO_MODE,
    NavanUnavailable,
    fetch_navan_bookings,
    navan_mode,
    summarize_events,
)
from services.spend_service import AUDIT_ENTITY as SPEND_AUDIT_ENTITY

logger = logging.getLogger(__name__)

SYNC_NAME = "navan_costs"
# Demo mode keeps its own run state, so a demo run never changes what the live sync remembers about real events
DEMO_SYNC_NAME = "navan_costs_demo"
SPEND_SOURCE = "NAVAN"
# The cost category of each booking type's line (services.navan_service.booking_type)
BOOKING_TYPE_CATEGORIES = {
    "FLIGHT": "FLIGHTS", "HOTEL": "HOTELS", "CAR": "GROUND_TRANSPORT", "RAIL": "OTHER_TRAVEL", "OTHER": "OTHER_TRAVEL",
}
# How each booking type reads in a line's description
_TYPE_LABELS = {"FLIGHT": "flights", "HOTEL": "hotels", "CAR": "cars", "RAIL": "rail", "OTHER": "other travel"}
VENDOR = "Navan"
STATUS = "INVOICED"
DEFAULT_SYNC_INTERVAL_MINUTES = 360
MIN_SYNC_INTERVAL_MINUTES = 5
ALREADY_RUNNING_MESSAGE = "A Navan sync is already running. Try again in a minute."

# One lock for both modes: they write the same kind of lines
_SYNC_LOCK_KEY = f"integration_sync_state:{SYNC_NAME}"
_SYNCED_FIELDS = ("description", "amount", "incurred_on")
_run_lock = asyncio.Lock()

# $1: only events whose name starts with this (demo mode), or null for every event
_EVENTS_SQL = """
    SELECT i.id, i.name, i.currency, ed.navan_event_name, ed.navan_estimate_amount
    FROM initiatives i
    JOIN channels c ON c.id = i.channel_id AND c.code = 'EVENTS'
    JOIN event_details ed ON ed.initiative_id = i.id
    WHERE $1::text IS NULL OR left(i.name, length($1::text)) = $1::text
"""


def _sync_name() -> str:
    return DEMO_SYNC_NAME if navan_mode() == DEMO_MODE else SYNC_NAME


class NavanSyncAlreadyRunning(Exception):
    """Another Navan sync is running (here or on another server). The message is safe to show to users."""

    def __init__(self, message: str = ALREADY_RUNNING_MESSAGE):
        super().__init__(message)


def navan_cost_sync_enabled() -> bool:
    return os.getenv("NAVAN_COST_SYNC_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _sync_interval_seconds() -> int:
    try:
        minutes = int(os.getenv("NAVAN_COST_SYNC_INTERVAL_MINUTES", str(DEFAULT_SYNC_INTERVAL_MINUTES)))
    except (TypeError, ValueError):
        minutes = DEFAULT_SYNC_INTERVAL_MINUTES
    return max(MIN_SYNC_INTERVAL_MINUTES, minutes) * 60


def link_events(navan_events: Dict[str, Dict[str, Any]], our_events) -> Dict[str, Dict[str, Any]]:
    """Navan event key (lower-cased name) → {"initiative_id", "initiative_name", "link": "SET" | "NAME_MATCH"}."""
    links: Dict[str, Dict[str, Any]] = {}
    for e in our_events:
        if e["navan_event_name"]:
            links[e["navan_event_name"].strip().lower()] = {
                "initiative_id": e["id"], "initiative_name": e["name"], "link": "SET",
            }
    by_name: Dict[str, List[Any]] = {}
    for e in our_events:
        by_name.setdefault(e["name"].strip().lower(), []).append(e)
    for key in navan_events:
        if key in links:
            continue
        candidates = [e for e in by_name.get(key, []) if not e["navan_event_name"]]
        if len(by_name.get(key, [])) == 1 and len(candidates) == 1:
            links[key] = {"initiative_id": candidates[0]["id"], "initiative_name": candidates[0]["name"], "link": "NAME_MATCH"}
    return links


def navan_create_events() -> bool:
    """Off unless NAVAN_CREATE_EVENTS is set: events are created here and linked to their Navan event."""
    return os.getenv("NAVAN_CREATE_EVENTS", "false").strip().lower() in {"1", "true", "yes", "on"}


def _status_for(start: Optional[datetime.date], end: Optional[datetime.date], today: datetime.date) -> str:
    if start is None or start > today:
        return "PLANNED"
    return "COMPLETED" if (end or start) < today else "ACTIVE"


async def _create_events(conn, navan_events, links, summary: Dict[str, Any]) -> None:
    """A portal event for each Navan event with active bookings that isn't linked and whose name isn't taken."""
    taken = {r["n"] for r in await conn.fetch(
        "SELECT lower(name) AS n FROM initiatives UNION "
        "SELECT lower(navan_event_name) FROM event_details WHERE navan_event_name IS NOT NULL"
    )}
    ids = await conn.fetchrow(
        "SELECT (SELECT id FROM brands WHERE code = 'TBD_ALL') AS brand, "
        "(SELECT id FROM channels WHERE code = 'EVENTS') AS channel, "
        "(SELECT id FROM sub_channels WHERE code = 'IN_PERSON_EVENTS') AS sub_channel"
    )
    if ids["brand"] is None or ids["channel"] is None:
        return
    today = datetime.date.today()
    for key, event in navan_events.items():
        if key in links or key in taken or not event["totals"]:
            continue
        currency = max(event["totals"], key=lambda c: event["totals"][c])
        status = _status_for(event["first_travel"], event["last_travel"], today)
        initiative_id = await conn.fetchval(
            """
            INSERT INTO initiatives (name, brand_id, channel_id, sub_channel_id, start_date, end_date, currency, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
            """,
            event["name"][:255], ids["brand"], ids["channel"], ids["sub_channel"], event["first_travel"],
            event["last_travel"], currency, status,
        )
        event_code = await conn.fetchval(
            "INSERT INTO event_details (initiative_id, city, country_code, navan_event_name) VALUES ($1, $2, $3, $4) "
            "RETURNING event_code",
            initiative_id, event["city"], event["country_code"], event["name"][:255],
        )
        values = {
            "name": event["name"], "status": status, "start_date": event["first_travel"],
            "end_date": event["last_travel"], "currency": currency,
            "event": {"event_code": event_code, "city": event["city"], "country_code": event["country_code"],
                      "navan_event_name": event["name"]},
            "source": "NAVAN",
        }
        await conn.execute(
            """
            INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, old_value, new_value, created_at)
            VALUES (NULL, 'CREATE', $1, $2, NULL, CAST($3 AS jsonb), now())
            """,
            INITIATIVE_AUDIT_ENTITY, str(initiative_id), json.dumps(values, default=str),
        )
        links[key] = {"initiative_id": initiative_id, "initiative_name": event["name"], "link": "SET"}
        taken.add(key)
        summary["events_created"] += 1


def _external_ref(initiative_id, kind: str, currency: str) -> str:
    return f"navan:{initiative_id}:{kind}:{currency}"


def _old_format_currency(ref: str) -> Optional[str]:
    """The currency of a line from before the split by booking type ("navan:<id>:<currency>"), else None."""
    parts = ref.split(":")
    return parts[2] if len(parts) == 3 else None


def _description(kind: str, bookings: int) -> str:
    return f"Navan {_TYPE_LABELS[kind]} ({bookings} {'booking' if bookings == 1 else 'bookings'})"


def _navan_key(event) -> str:
    """The Navan event an event of ours takes its bookings from: its "Navan event", else its own name."""
    return (event["navan_event_name"] or event["name"]).strip().lower()


def _keep_absent_line(have, navan_events, links, our_keys: Dict[Any, str], previous_keys: Dict[str, str]) -> bool:
    """Whether a Navan cost line no longer in this run's totals should stay as it is.

    Bookings are only read for the lookback window, so a Navan event whose bookings are all older is missing from
    the run, not cancelled: its event keeps its line. The line goes when the event's bookings were read (they're
    all cancelled now), or the event is no longer linked to that Navan event (it isn't an event any more, its
    "Navan event" changed since the last run, or another event claimed that Navan event).
    """
    key = our_keys.get(have["initiative_id"])
    if key is None or key in navan_events:
        return False
    previous = previous_keys.get(str(have["initiative_id"]))
    if previous is not None and previous != key:
        return False
    link = links.get(key)
    return link is None or link["initiative_id"] == have["initiative_id"]


async def _previous_links(conn) -> Dict[str, str]:
    """Initiative id → the Navan event (lower-cased name) it was linked to in the last successful run."""
    raw = await conn.fetchval("SELECT last_run_summary FROM integration_sync_state WHERE name = $1", _sync_name())
    # Older summaries have no line_links: fall back to the Navan events they linked
    last = json.loads(raw) if isinstance(raw, str) else (raw or {})
    if isinstance(last.get("line_links"), dict):
        return last["line_links"]
    return {
        e["initiative_id"]: e["name"].strip().lower()
        for e in last.get("events", [])
        if e.get("initiative_id") and e.get("name")
    }


async def _audit(conn, audits) -> None:
    for entity_type, action, entity_id, old, new in audits:
        # The actor is empty: Navan made the change
        await conn.execute(
            """
            INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, old_value, new_value, created_at)
            VALUES (NULL, $1, $2, $3, CAST($4 AS jsonb), CAST($5 AS jsonb), now())
            """,
            action, entity_type, str(entity_id),
            json.dumps(old, default=str) if old is not None else None,
            json.dumps(new, default=str) if new is not None else None,
        )


async def _apply_estimates(conn, estimates, links, our_events, summary: Dict[str, Any], audits: list) -> None:
    """Set each linked event's Navan estimate to the one the source gave, when it's in the event's currency."""
    by_id = {e["id"]: e for e in our_events}
    for key, estimate in estimates.items():
        link = links.get(key)
        event = by_id.get(link["initiative_id"]) if link else None
        if event is None or estimate["currency"] != event["currency"]:
            continue
        amount = Decimal(estimate["amount"]).quantize(Decimal("0.01"))
        if event["navan_estimate_amount"] == amount:
            continue
        await conn.execute(
            "UPDATE event_details SET navan_estimate_amount = $2, updated_at = clock_timestamp() WHERE initiative_id = $1",
            event["id"], amount,
        )
        audits.append((
            INITIATIVE_AUDIT_ENTITY, "UPDATE", event["id"],
            {"event": {"navan_estimate_amount": event["navan_estimate_amount"]}},
            {"event": {"navan_estimate_amount": amount}, "source": SPEND_SOURCE},
        ))
        summary["estimates_updated"] += 1


async def _apply(conn, navan_events, summary: Dict[str, Any], estimates: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    demo = navan_mode() == DEMO_MODE
    wanted = sorted(set(BOOKING_TYPE_CATEGORIES.values()))
    categories = {
        r["code"]: r["id"]
        for r in await conn.fetch("SELECT id, code FROM spend_categories WHERE code = ANY($1::text[])", wanted)
    }
    missing = [c for c in wanted if c not in categories]
    if missing:
        raise RuntimeError(f"Spend categories {', '.join(missing)} are missing")
    # Demo mode only sees the demo events, so it can't link, change or remove anything of a real event
    our_events = await conn.fetch(_EVENTS_SQL, DEMO_EVENT_PREFIX if demo else None)
    our_keys = {e["id"]: _navan_key(e) for e in our_events}
    previous_keys = await _previous_links(conn)
    links = link_events(navan_events, our_events)
    if navan_create_events() and not demo:
        await _create_events(conn, navan_events, links, summary)
    today = datetime.date.today()
    estimates = estimates or {}

    desired: Dict[str, Dict[str, Any]] = {}
    # Initiative id → the Navan event its cost lines come from, kept for the next run (see _keep_absent_line)
    line_links: Dict[str, str] = {}
    for key, event in navan_events.items():
        link = links.get(key)
        estimate = estimates.get(key)
        summary["events"].append({
            "name": event["name"],
            "bookings": event["bookings"],
            "totals": [{"currency": c, "amount": float(a)} for c, a in sorted(event["totals"].items())],
            "by_type": [
                {"booking_type": kind, "currency": currency, "amount": float(part["amount"]), "bookings": part["bookings"]}
                for (kind, currency), part in sorted(event["by_type"].items())
            ],
            "estimate": {"currency": estimate["currency"], "amount": float(estimate["amount"])} if estimate else None,
            "first_travel": event["first_travel"].isoformat() if event["first_travel"] else None,
            "initiative_id": str(link["initiative_id"]) if link else None,
            "initiative_name": link["initiative_name"] if link else None,
            "link": link["link"] if link else None,
        })
        if not link:
            continue
        line_links[str(link["initiative_id"])] = key
        for (kind, currency), part in event["by_type"].items():
            if part["amount"] == 0:
                continue
            ref = _external_ref(link["initiative_id"], kind, currency)
            desired[ref] = {
                "initiative_id": link["initiative_id"], "currency": currency,
                "amount": part["amount"].quantize(Decimal("0.01")), "booking_type": kind,
                "category_id": categories[BOOKING_TYPE_CATEGORIES[kind]],
                "description": _description(kind, part["bookings"]), "incurred_on": event["first_travel"] or today,
            }
    summary["line_links"] = line_links
    summary["events"].sort(key=lambda e: (e["initiative_id"] is None, e["first_travel"] or "", e["name"]))
    summary["linked"] = sum(1 for e in summary["events"] if e["initiative_id"])

    current = {
        r["external_ref"]: r
        for r in await conn.fetch(
            """
            SELECT id, initiative_id, external_ref, description, amount, currency, incurred_on, reconciled_month
            FROM spend_lines WHERE source = $1 AND ($2::uuid[] IS NULL OR initiative_id = ANY($2::uuid[]))
            FOR UPDATE
            """,
            SPEND_SOURCE, [e["id"] for e in our_events] if demo else None,
        )
    }

    # A reconciled line from before the split stays, so its event and currency get no per-type lines on top of it
    frozen = {
        (have["initiative_id"], _old_format_currency(ref))
        for ref, have in current.items()
        if _old_format_currency(ref) and have["reconciled_month"] is not None
    }
    desired = {ref: want for ref, want in desired.items() if (want["initiative_id"], want["currency"]) not in frozen}

    audits = []
    for ref, want in desired.items():
        have = current.get(ref)
        if have is None:
            line_id = await conn.fetchval(
                """
                INSERT INTO spend_lines (initiative_id, category_id, description, vendor, status, amount, currency,
                                         incurred_on, cost_type, source, external_ref)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'OPEX', $9, $10)
                RETURNING id
                """,
                want["initiative_id"], want["category_id"], want["description"], VENDOR, STATUS, want["amount"],
                want["currency"], want["incurred_on"], SPEND_SOURCE, ref,
            )
            audits.append((SPEND_AUDIT_ENTITY, "CREATE", line_id, None, {**want, "source": SPEND_SOURCE, "external_ref": ref}))
            summary["created"] += 1
            continue
        changed = {f: want[f] for f in _SYNCED_FIELDS if have[f] != want[f]}
        if not changed:
            continue
        if have["reconciled_month"] is not None and set(changed) - {"description"}:
            summary["conflicts"] += 1
            continue
        await conn.execute(
            "UPDATE spend_lines SET description = $2, amount = $3, incurred_on = $4, updated_at = now() WHERE id = $1",
            have["id"], want["description"], want["amount"], want["incurred_on"],
        )
        audits.append((SPEND_AUDIT_ENTITY, "UPDATE", have["id"], {f: have[f] for f in changed}, changed))
        summary["updated"] += 1

    for ref, have in current.items():
        if ref in desired:
            continue
        if _keep_absent_line(have, navan_events, links, our_keys, previous_keys):
            line_links[str(have["initiative_id"])] = our_keys[have["initiative_id"]]
            continue
        if have["reconciled_month"] is not None:
            previous = previous_keys.get(str(have["initiative_id"]))
            if previous is not None:
                line_links.setdefault(str(have["initiative_id"]), previous)
            summary["conflicts"] += 1
            continue
        await conn.execute("DELETE FROM spend_lines WHERE id = $1", have["id"])
        audits.append((SPEND_AUDIT_ENTITY, "DELETE", have["id"], {"amount": have["amount"], "currency": have["currency"],
                                                                  "source": SPEND_SOURCE, "external_ref": ref}, None))
        summary["removed"] += 1

    await _apply_estimates(conn, estimates, links, our_events, summary, audits)
    await _audit(conn, audits)


def _empty_summary() -> Dict[str, Any]:
    return {
        "mode": navan_mode(), "bookings": 0, "linked": 0, "events_created": 0, "created": 0, "updated": 0,
        "removed": 0, "conflicts": 0, "estimates_updated": 0, "events": [],
    }


async def _record_failure(message: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO integration_sync_state (name, last_attempt_at, last_error)
            VALUES ($1, now(), $2)
            ON CONFLICT (name) DO UPDATE SET last_attempt_at = now(), last_error = EXCLUDED.last_error
            """,
            _sync_name(), message,
        )


async def _read_source() -> Dict[str, Any]:
    """The bookings (and any estimates per Navan event) to sync: from Navan, or generated in demo mode."""
    if navan_mode() == DEMO_MODE:
        return await fetch_demo_navan_data()
    return {"bookings": await fetch_navan_bookings(), "estimates": {}}


async def _run_sync() -> Dict[str, Any]:
    summary = _empty_summary()
    # Navan first, with no database connection held
    source = await _read_source()
    bookings, estimates = source["bookings"], source.get("estimates") or {}
    summary["bookings"] = len(bookings)
    navan_events = summarize_events(bookings)
    # An event with an estimate but nothing booked yet is still known to the source
    for key, estimate in estimates.items():
        navan_events.setdefault(key, {
            "name": estimate["name"], "bookings": 0, "first_travel": None, "last_travel": None, "city": None,
            "country_code": None, "totals": {}, "by_type": {},
        })

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval("SELECT pg_try_advisory_xact_lock(hashtext($1))", _SYNC_LOCK_KEY):
                raise NavanSyncAlreadyRunning()
            await _apply(conn, navan_events, summary, estimates)
            await conn.execute(
                """
                INSERT INTO integration_sync_state (name, last_attempt_at, last_success_at, last_error, last_run_summary)
                VALUES ($1, now(), now(), NULL, CAST($2 AS jsonb))
                ON CONFLICT (name) DO UPDATE SET last_attempt_at = now(), last_success_at = now(), last_error = NULL,
                    last_run_summary = EXCLUDED.last_run_summary
                """,
                _sync_name(), json.dumps(summary, default=str),
            )
    return summary


async def sync_navan_costs() -> Dict[str, Any]:
    """Run one sync and return its summary. Raises NavanSyncAlreadyRunning or NavanUnavailable (stored)."""
    if _run_lock.locked():
        raise NavanSyncAlreadyRunning()
    async with _run_lock:
        try:
            summary = await _run_sync()
        except NavanSyncAlreadyRunning:
            raise
        except NavanUnavailable as exc:
            logger.warning(f"Navan cost sync could not reach Navan: {exc}", extra={"event": "navan_cost_sync_unavailable"})
            await _record_failure(str(exc))
            raise
        except Exception as exc:
            logger.exception(f"Navan cost sync failed: {type(exc).__name__}: {exc}", extra={"event": "navan_cost_sync_failed"})
            await _record_failure(f"The sync stopped with an unexpected error ({type(exc).__name__}). Nothing was changed.")
            raise
    logger.info(
        "Navan cost sync completed",
        extra={"event": "navan_cost_sync_completed", "summary": {k: v for k, v in summary.items() if k not in ("events", "line_links")}},
    )
    return summary


async def get_navan_sync_state() -> Optional[Dict[str, Any]]:
    """The stored state of the Navan sync in the current mode (live or demo), or None before its first run."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_attempt_at, last_success_at, last_error, last_run_summary FROM integration_sync_state WHERE name = $1",
            _sync_name(),
        )
    if row is None:
        return None
    state = dict(row)
    if isinstance(state["last_run_summary"], str):
        state["last_run_summary"] = json.loads(state["last_run_summary"])
    return state


async def navan_cost_sync_loop(interval_seconds: Optional[int] = None) -> None:
    """Run sync_navan_costs() forever on a fixed interval; a failed run is logged and retried next interval."""
    interval = interval_seconds or _sync_interval_seconds()
    while True:
        try:
            await sync_navan_costs()
        except (NavanSyncAlreadyRunning, NavanUnavailable):
            pass
        except Exception:
            logger.exception("Navan cost sync iteration crashed; will retry next interval",
                             extra={"event": "navan_cost_sync_iteration_crashed"})
        await asyncio.sleep(interval)

