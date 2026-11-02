"""Keeps hubspot_campaigns the same as HubSpot: every campaign with its properties, report figures and assets.

Runs as a background asyncio task started from server.py's lifespan when HUBSPOT_SYNC_ENABLED is on, every
HUBSPOT_SYNC_INTERVAL_MINUTES, like services/navan_cost_sync_job.py. It can also be started on demand from
POST /api/integrations/hubspot/sync, which returns at once: reading every campaign's report and assets takes a few
minutes at HubSpot's rate limit.

How a run works:
- Reads everything from HubSpot with no database connection held (services/hubspot/hubspot_client.py).
- In one transaction: replaces every campaign's row, and deletes the rows of campaigns HubSpot no longer has.
  HubSpot owns this data, so nothing here is edited by hand and nothing is kept.
- In the same transaction, each campaign is an initiative too, linked to it (initiative_links, system HUBSPOT,
  external_id = the campaign GUID):
  - A campaign with no initiative gets one when HUBSPOT_CREATE_INITIATIVES is on (default): named as in HubSpot,
    with its dates, budget and currency, brand from the code its name starts with (CAMPAIGN_BRANDS, else TBD/All),
    channel guessed from its name (_channel_for, else Other), status from HubSpot's (else from its dates). Nobody is
    recorded as creating it, and the audit log has it with source HUBSPOT.
  - An initiative the sync created keeps its name, dates, status and budget in step with HubSpot. Brand, channel and
    everything else are left as people set them. Initiatives people created and linked are never changed.
  - Initiatives stay when their campaign leaves HubSpot: they can hold costs and results.
- Records the run in integration_sync_state ("hubspot_campaigns"). A failed run changes nothing and keeps its
  error there until the next successful one.
"""

import asyncio
import datetime
import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from postgresql_db.database import get_pool
from services.shared import ads_service
from services.hubspot.hubspot_client import HubSpotUnavailable, fetch_hubspot_campaigns
from services.shared.initiative_service import AUDIT_ENTITY as INITIATIVE_AUDIT_ENTITY

logger = logging.getLogger(__name__)

SYNC_NAME = "hubspot_campaigns"
DEFAULT_SYNC_INTERVAL_MINUTES = 360
MIN_SYNC_INTERVAL_MINUTES = 5
ALREADY_RUNNING_MESSAGE = "A HubSpot sync is already running. It takes a few minutes."

_SYNC_LOCK_KEY = f"integration_sync_state:{SYNC_NAME}"
_run_lock = asyncio.Lock()
# The sync started from the API, kept so it isn't garbage collected while it runs
_background: Optional[asyncio.Task] = None

_UPSERT_SQL = """
    INSERT INTO hubspot_campaigns (
        id, name, status, start_date, end_date, currency, budget_total, spend_total, owner_name, utm, notes, audience,
        sessions, new_contacts_first_touch, new_contacts_last_touch, influenced_contacts, list_contacts, assets, funnel,
        hubspot_created_at, hubspot_updated_at, synced_at
    )
    SELECT u.id, u.name, u.status, u.start_date, u.end_date, u.currency, u.budget_total, u.spend_total, u.owner_name,
           u.utm, u.notes, u.audience, u.sessions, u.new_first, u.new_last, u.influenced, u.list_contacts,
           u.assets::jsonb, u.funnel::jsonb, u.created_at, u.updated_at, now()
    FROM unnest(
        $1::varchar[], $2::varchar[], $3::varchar[], $4::date[], $5::date[], $6::varchar[], $7::numeric[],
        $8::numeric[], $9::varchar[], $10::varchar[], $11::text[], $12::text[], $13::int[], $14::int[], $15::int[],
        $16::int[], $17::int[], $18::text[], $19::text[], $20::timestamptz[], $21::timestamptz[]
    ) AS u(id, name, status, start_date, end_date, currency, budget_total, spend_total, owner_name, utm, notes,
           audience, sessions, new_first, new_last, influenced, list_contacts, assets, funnel, created_at, updated_at)
    ON CONFLICT (id) DO UPDATE SET
        name = EXCLUDED.name, status = EXCLUDED.status, start_date = EXCLUDED.start_date,
        end_date = EXCLUDED.end_date, currency = EXCLUDED.currency, budget_total = EXCLUDED.budget_total,
        spend_total = EXCLUDED.spend_total, owner_name = EXCLUDED.owner_name, utm = EXCLUDED.utm,
        notes = EXCLUDED.notes, audience = EXCLUDED.audience, sessions = EXCLUDED.sessions,
        new_contacts_first_touch = EXCLUDED.new_contacts_first_touch,
        new_contacts_last_touch = EXCLUDED.new_contacts_last_touch,
        influenced_contacts = EXCLUDED.influenced_contacts, list_contacts = EXCLUDED.list_contacts,
        assets = EXCLUDED.assets, funnel = EXCLUDED.funnel,
        hubspot_created_at = EXCLUDED.hubspot_created_at, hubspot_updated_at = EXCLUDED.hubspot_updated_at,
        synced_at = now()
    RETURNING (xmax = 0) AS inserted
"""

_COLUMNS = (
    "id", "name", "status", "start_date", "end_date", "currency", "budget_total", "spend_total", "owner_name", "utm",
    "notes", "audience", "sessions", "new_contacts_first_touch", "new_contacts_last_touch", "influenced_contacts",
    "list_contacts",
)

LINK_SYSTEM = "HUBSPOT"
DEFAULT_BRAND = "TBD_ALL"
DEFAULT_CURRENCY = "USD"
# The code a campaign name starts with ("DS - ...", "[OPS] - ...") and the brand (brands.code) it stands for
CAMPAIGN_BRANDS = {
    "DS": "DAAS", "DAAS": "DAAS", "ZD": "ZENADRONE", "ZT": "ZENATECH", "TS": "TILLERSTACK", "WA": "WORKAWARE",
    "WA/AU": "WORKAWARE", "DF": "DESKFLEX", "OS": "OTHERSHIP", "K9": "K9SKY", "JD": "JADIAN", "ZO": "ZOO_OFFICE",
}
# The first word of the name, before a dash or a space ("TS UK - PPC" is TS)
_CODE = re.compile(r"^\s*\[?([A-Za-z0-9/]{1,6})\]?(?=[\s\-–—�])")
# The first rule whose words appear in the campaign name gives its channel and sub-channel (channels.code,
# sub_channels.code); no match is Other
_CHANNEL_RULES: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"\bwebinars?\b"), "EVENTS", "WEBINARS"),
    (re.compile(r"\b(evt|events?|trade ?shows?|shows?|expo|summit|conference|exhibition|booth|tradeshow|championships?"
                r"|tournament|fair|forum|congress)\b"), "EVENTS", "IN_PERSON_EVENTS"),
    (re.compile(r"\bbing\b"), "PAID_SEARCH", "BING_ADS"),
    (re.compile(r"\b(google|ppc|sem|paid search)\b"), "PAID_SEARCH", "GOOGLE_ADS"),
    (re.compile(r"\blinkedin (ads|paid)\b|\bpaid linkedin\b"), "PAID_SOCIAL", "LINKEDIN_ADS"),
    (re.compile(r"\b(meta|facebook ads?|instagram ads?)\b"), "PAID_SOCIAL", "META_ADS"),
    (re.compile(r"\bpaid\b"), "PAID_SEARCH", "OTHER"),
    (re.compile(r"\bnewsletters?\b"), "EMAIL_MARKETING", "EMAIL_NEWSLETTER"),
    (re.compile(r"\b(email|e-mail|nurture|sequence)\b"), "EMAIL_MARKETING", "EMAIL_NURTURE"),
    (re.compile(r"\bseo\b"), "SEO", "ORGANIC_SEARCH"),
    (re.compile(r"\bblog\b"), "CONTENT_MARKETING", "BLOG"),
    (re.compile(r"\b(testimonials?|content|case stud(y|ies)|videos?|perspective|podcasts?|listicles?)\b"), "CONTENT_MARKETING", "OTHER"),
    (re.compile(r"\b(organic|social)\b"), "ORGANIC_SOCIAL", "OTHER"),
    (re.compile(r"\b(partners?|partnerships?|resellers?|referrals?)\b"), "PARTNERSHIPS", "OTHER"),
]
_FALLBACK_CHANNEL = ("OTHER", "OTHER")
# The currency of the countries the events run in (country_code -> currency); a country not here gives no currency
_EURO_COUNTRIES = (
    "AT BE CY DE EE ES FI FR GR HR IE IT LT LU LV MT NL PT SI SK"
).split()
_COUNTRY_CURRENCY = {
    **dict.fromkeys(_EURO_COUNTRIES, "EUR"),
    "US": "USD", "CA": "CAD", "GB": "GBP", "AU": "AUD", "NZ": "NZD", "CH": "CHF", "SE": "SEK", "NO": "NOK",
    "DK": "DKK", "PL": "PLN", "CZ": "CZK", "KR": "KRW", "JP": "JPY", "IN": "INR", "SG": "SGD", "AE": "AED",
    "MX": "MXN", "BR": "BRL", "ZA": "ZAR",
}
_STATUSES = {"planned": "PLANNED", "in_progress": "ACTIVE", "active": "ACTIVE", "paused": "PAUSED", "completed": "COMPLETED"}
# What the sync keeps in step on the initiatives it created
_KEPT_FIELDS = ("name", "start_date", "end_date", "status", "budget_amount")


class HubSpotSyncAlreadyRunning(Exception):
    """Another HubSpot sync is running (here or on another server). The message is safe to show to users."""

    def __init__(self, message: str = ALREADY_RUNNING_MESSAGE):
        super().__init__(message)


def hubspot_sync_enabled() -> bool:
    return (os.getenv("HUBSPOT_SYNC_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def sync_interval_minutes() -> int:
    try:
        minutes = int(os.getenv("HUBSPOT_SYNC_INTERVAL_MINUTES") or DEFAULT_SYNC_INTERVAL_MINUTES)
    except ValueError:
        minutes = DEFAULT_SYNC_INTERVAL_MINUTES
    return max(MIN_SYNC_INTERVAL_MINUTES, minutes)


def hubspot_create_initiatives() -> bool:
    return (os.getenv("HUBSPOT_CREATE_INITIATIVES") or "true").strip().lower() in ("1", "true", "yes", "on")


def brand_for(name: str) -> str:
    """The brand code (brands.code) a campaign belongs to, from the code its name starts with; else TBD/All."""
    match = _CODE.match(name)
    code = match.group(1).upper() if match else ("DAAS" if re.match(r"\s*daas\b", name, re.IGNORECASE) else None)
    return CAMPAIGN_BRANDS.get(code or "", DEFAULT_BRAND)


def channel_for(campaign: Dict[str, Any]) -> Tuple[str, str]:
    """(channel code, sub-channel code) guessed from the campaign's name; a campaign with marketing events is an event."""
    name = campaign["name"].lower()
    for pattern, channel, sub_channel in _CHANNEL_RULES:
        if pattern.search(name):
            return channel, sub_channel
    if campaign["assets"].get("MARKETING_EVENT"):
        return "EVENTS", "IN_PERSON_EVENTS"
    return _FALLBACK_CHANNEL


def effective_currency(hubspot_currency: Optional[str], country_code: Optional[str],
                       initiative_currency: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(currency, where it came from): HubSpot's own, else that of the country the linked event is in, else the linked
    initiative's. None when none of them is known."""
    if hubspot_currency:
        return hubspot_currency, "HUBSPOT"
    by_country = _COUNTRY_CURRENCY.get((country_code or "").strip().upper())
    if by_country:
        return by_country, "COUNTRY"
    if initiative_currency:
        return initiative_currency.strip().upper(), "INITIATIVE"
    return None, None


def initiative_status(campaign: Dict[str, Any], today: datetime.date) -> str:
    """HubSpot's status as an initiative status; when it isn't set, from the dates."""
    if campaign["status"] in _STATUSES:
        return _STATUSES[campaign["status"]]
    start, end = campaign["start_date"], campaign["end_date"]
    if end and end < today:
        return "COMPLETED"
    if start and start <= today:
        return "ACTIVE"
    return "PLANNED"


def _initiative_fields(campaign: Dict[str, Any], today: datetime.date) -> Dict[str, Any]:
    """The fields of a campaign's initiative that come from HubSpot."""
    return {
        "name": campaign["name"][:255],
        "start_date": campaign["start_date"],
        "end_date": campaign["end_date"],
        "status": initiative_status(campaign, today),
        "budget_amount": campaign["budget_total"],
    }


async def _sync_initiatives(conn, rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    """Create an initiative for each campaign without one (when on), and keep the sync's own in step with HubSpot."""
    today = datetime.date.today()
    linked = {
        r["external_id"]: dict(r)
        for r in await conn.fetch(
            """
            SELECT l.external_id, i.id, i.name, i.start_date, i.end_date, i.status, i.budget_amount, i.created_by
            FROM initiative_links l JOIN initiatives i ON i.id = l.initiative_id
            WHERE l.system = $1 AND l.external_id = ANY($2::text[])
            FOR UPDATE OF i
            """,
            LINK_SYSTEM, [row["id"] for row in rows],
        )
    }
    brands = {r["code"]: r["id"] for r in await conn.fetch("SELECT id, code FROM brands")}
    channels = {
        (r["channel"], r["sub_channel"]): (r["channel_id"], r["sub_channel_id"])
        for r in await conn.fetch(
            """
            SELECT c.code AS channel, s.code AS sub_channel, c.id AS channel_id, s.id AS sub_channel_id
            FROM channel_sub_channels cs JOIN channels c ON c.id = cs.channel_id JOIN sub_channels s ON s.id = cs.sub_channel_id
            """
        )
    }
    if DEFAULT_BRAND not in brands or _FALLBACK_CHANNEL not in channels:
        raise RuntimeError("The TBD/All brand or the Other / Other channel is missing")

    creates, links, updates, audits = [], [], [], []
    create_new = hubspot_create_initiatives()
    for campaign in rows:
        want = _initiative_fields(campaign, today)
        have = linked.get(campaign["id"])
        if have is None:
            if not create_new:
                continue
            initiative_id = uuid.uuid4()
            channel_id, sub_channel_id = channels.get(channel_for(campaign), channels[_FALLBACK_CHANNEL])
            row = {
                "id": str(initiative_id), **want,
                "brand_id": brands.get(brand_for(campaign["name"]), brands[DEFAULT_BRAND]),
                "channel_id": channel_id, "sub_channel_id": sub_channel_id,
                "currency": campaign["currency"] or DEFAULT_CURRENCY,
                "description": "Added by the HubSpot sync from the HubSpot campaign of the same name.",
            }
            creates.append(row)
            links.append({"initiative_id": str(initiative_id), "external_id": campaign["id"]})
            audits.append(("CREATE", initiative_id, None, {**{k: v for k, v in row.items() if k != "id"},
                                                           "hubspot_campaign_id": campaign["id"], "source": LINK_SYSTEM}))
        elif have["created_by"] is None:
            changed = {k: v for k, v in want.items() if have[k] != v}
            if changed:
                updates.append({"id": str(have["id"]), **{k: have[k] for k in _KEPT_FIELDS}, **changed})
                audits.append(("UPDATE", have["id"], {k: have[k] for k in changed}, {**changed, "source": LINK_SYSTEM}))

    if creates:
        await conn.execute(
            """
            INSERT INTO initiatives (id, name, brand_id, channel_id, sub_channel_id, start_date, end_date, budget_amount,
                                     currency, status, data_source, description)
            SELECT t.id, t.name, t.brand_id, t.channel_id, t.sub_channel_id, t.start_date, t.end_date, t.budget_amount,
                   t.currency, t.status, 'LINKED', t.description
            FROM jsonb_to_recordset(CAST($1 AS jsonb)) AS t(
                id uuid, name text, brand_id int, channel_id int, sub_channel_id int, start_date date, end_date date,
                budget_amount numeric, currency text, status text, description text)
            """,
            json.dumps(creates, default=str),
        )
        await conn.execute(
            """
            INSERT INTO initiative_links (initiative_id, system, external_id)
            SELECT t.initiative_id, $2, t.external_id
            FROM jsonb_to_recordset(CAST($1 AS jsonb)) AS t(initiative_id uuid, external_id text)
            ON CONFLICT (system, external_id) DO NOTHING
            """,
            json.dumps(links), LINK_SYSTEM,
        )
    if updates:
        await conn.execute(
            """
            UPDATE initiatives i
            SET name = t.name, start_date = t.start_date, end_date = t.end_date, status = t.status,
                budget_amount = t.budget_amount, updated_at = now()
            FROM jsonb_to_recordset(CAST($1 AS jsonb)) AS t(
                id uuid, name text, start_date date, end_date date, status text, budget_amount numeric)
            WHERE i.id = t.id
            """,
            json.dumps(updates, default=str),
        )
    await ads_service.audit_many(conn, None, INITIATIVE_AUDIT_ENTITY, audits)
    summary["initiatives_created"] = len(creates)
    summary["initiatives_updated"] = len(updates)


def hubspot_sync_running() -> bool:
    return _run_lock.locked() or (_background is not None and not _background.done())


async def _store(conn, rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    params = [[row[column] for row in rows] for column in _COLUMNS]
    params.append([json.dumps(row["assets"]) for row in rows])
    params.append([json.dumps(row["funnel"]) for row in rows])
    params.append([row["hubspot_created_at"] for row in rows])
    params.append([row["hubspot_updated_at"] for row in rows])
    stored = await conn.fetch(_UPSERT_SQL, *params) if rows else []
    summary["created"] = sum(1 for r in stored if r["inserted"])
    summary["updated"] = len(stored) - summary["created"]
    removed = await conn.execute("DELETE FROM hubspot_campaigns WHERE NOT (id = ANY($1::varchar[]))", [row["id"] for row in rows])
    summary["removed"] = int(removed.split()[-1])


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


async def _run_sync() -> Dict[str, Any]:
    # HubSpot first, with no database connection held
    source = await fetch_hubspot_campaigns()
    rows = source["campaigns"]
    summary: Dict[str, Any] = {
        "campaigns": len(rows), "created": 0, "updated": 0, "removed": 0,
        "unreadable_asset_types": source["unreadable_asset_types"], "initiatives_created": 0, "initiatives_updated": 0,
        "without_budget": sum(1 for r in rows if r["budget_total"] is None),
        "funnel_error": source.get("funnel_error"),
    }
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval("SELECT pg_try_advisory_xact_lock(hashtext($1))", _SYNC_LOCK_KEY):
                raise HubSpotSyncAlreadyRunning()
            await _store(conn, rows, summary)
            await _sync_initiatives(conn, rows, summary)
            await conn.execute(
                """
                INSERT INTO integration_sync_state (name, last_attempt_at, last_success_at, last_error, last_run_summary)
                VALUES ($1, now(), now(), NULL, CAST($2 AS jsonb))
                ON CONFLICT (name) DO UPDATE SET last_attempt_at = now(), last_success_at = now(), last_error = NULL,
                    last_run_summary = EXCLUDED.last_run_summary
                """,
                SYNC_NAME, json.dumps(summary),
            )
    return summary


async def sync_hubspot_campaigns() -> Dict[str, Any]:
    """Run one sync and return its summary. Raises HubSpotSyncAlreadyRunning or HubSpotUnavailable (stored)."""
    if _run_lock.locked():
        raise HubSpotSyncAlreadyRunning()
    async with _run_lock:
        try:
            summary = await _run_sync()
        except HubSpotSyncAlreadyRunning:
            raise
        except HubSpotUnavailable as exc:
            logger.warning(f"HubSpot campaign sync could not read HubSpot: {exc}", extra={"event": "hubspot_sync_unavailable"})
            await _record_failure(str(exc))
            raise
        except Exception as exc:
            logger.exception(f"HubSpot campaign sync failed: {type(exc).__name__}: {exc}", extra={"event": "hubspot_sync_failed"})
            await _record_failure(f"The sync stopped with an unexpected error ({type(exc).__name__}). Nothing was changed.")
            raise
    logger.info("HubSpot campaign sync completed", extra={"event": "hubspot_sync_completed", "summary": summary})
    return summary


async def _sync_quietly() -> None:
    try:
        await sync_hubspot_campaigns()
    except Exception:
        # Already logged and stored by sync_hubspot_campaigns; the status shows it
        pass


def start_hubspot_sync() -> None:
    """Start a sync in the background and return at once. Raises HubSpotSyncAlreadyRunning."""
    global _background
    if hubspot_sync_running():
        raise HubSpotSyncAlreadyRunning()
    _background = asyncio.create_task(_sync_quietly(), name="hubspot-campaign-sync-now")


async def get_hubspot_sync_state() -> Optional[Dict[str, Any]]:
    """The stored state of the HubSpot sync, or None before its first run."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_attempt_at, last_success_at, last_error, last_run_summary FROM integration_sync_state WHERE name = $1",
            SYNC_NAME,
        )
    if row is None:
        return None
    state = dict(row)
    if isinstance(state["last_run_summary"], str):
        state["last_run_summary"] = json.loads(state["last_run_summary"])
    return state


async def list_hubspot_campaigns() -> List[Dict[str, Any]]:
    """Every synced campaign, newest start first, with the initiative linked to it (if any)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT hc.*, i.id::text AS initiative_id, i.name AS initiative_name, i.currency AS initiative_currency,
                   ed.country_code
            FROM hubspot_campaigns hc
            LEFT JOIN initiative_links il ON il.system = 'HUBSPOT' AND il.external_id = hc.id
            LEFT JOIN initiatives i ON i.id = il.initiative_id
            LEFT JOIN event_details ed ON ed.initiative_id = i.id
            ORDER BY hc.start_date DESC NULLS LAST, hc.name
            """
        )
    campaigns = []
    for row in rows:
        campaign = dict(row)
        campaign["currency"], campaign["currency_source"] = effective_currency(
            campaign["currency"], campaign.pop("country_code"), campaign.pop("initiative_currency"))
        for key in ("assets", "funnel"):
            if isinstance(campaign[key], str):
                campaign[key] = json.loads(campaign[key])
        campaigns.append(campaign)
    return campaigns


async def hubspot_sync_loop(interval_seconds: Optional[int] = None) -> None:
    """Run sync_hubspot_campaigns() forever on a fixed interval; a failed run is logged and retried next interval."""
    interval = interval_seconds or sync_interval_minutes() * 60
    while True:
        try:
            await sync_hubspot_campaigns()
        except (HubSpotSyncAlreadyRunning, HubSpotUnavailable):
            pass
        except Exception:
            logger.exception("HubSpot campaign sync iteration crashed; will retry next interval",
                             extra={"event": "hubspot_sync_iteration_crashed"})
        await asyncio.sleep(interval)
