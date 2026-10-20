"""Keeps Google Ads campaigns and their weekly results up to date from the connected Google Ads accounts.

Runs as a background asyncio task started from server.py's lifespan when GOOGLE_ADS_SYNC_ENABLED is on (off by
default), every GOOGLE_ADS_SYNC_INTERVAL_MINUTES (default 360), like services/navan_cost_sync_job.py. It can also be
run on demand from POST /api/integrations/google-ads/sync.

How a run works:
- Connections: every CONNECTED Google Ads connection (oauth_connections) made in the current GOOGLE_ADS_MODE. Mock
  connections are only read in mock mode and real ones only in live mode (services/google_ads_client.connection_mode).
  Each non-manager account in a connection's discovered accounts is read once, even when two connections list it
  (a company's own connection wins over the organization-wide one, for the brand).
- Reading, with no database connection held: each account's campaigns (id, name, status, type, dates, budget) and its
  metrics per week (cost, impressions, clicks, conversions) for the last GOOGLE_ADS_LOOKBACK_DAYS days (default 90,
  from the Monday of that week to today). Conversions change after the fact, so the whole window is read every run.
  Mock mode generates them (services/google_ads_mock_data.py); live mode uses the Google Ads API. An account that
  can't be read is noted in the summary and the others still sync; when no account can be read the run fails.
- Campaigns: a Google campaign is the portal campaign linked to it (initiative_links, system GOOGLE_ADS, external_id
  = the campaign id). Without one, and when GOOGLE_ADS_CREATE_CAMPAIGNS is on (default), a campaign is added: Paid
  Search / Google Ads, named as in Google, brand of the connection's company (else TBD/All), in the account's
  currency, with Google's dates and budget, objective Awareness for display and video campaigns and Leads otherwise,
  and linked to it with the account id. A removed campaign with nothing in the window isn't added.
  Linked campaigns follow Google on every run: status (Enabled -> Active, or Planned before its start and Completed
  after its end; Paused -> Paused; Removed -> Cancelled), dates and budget. The name only follows Google for
  campaigns the sync added (nobody is recorded as creating them); a name someone gave a campaign stays.
- Results: one ad result per campaign per week (no ad group, Monday to Sunday), source GOOGLE_ADS_API, created or
  updated in place by services/ads_service.apply_results, so re-runs never duplicate. Budget per week is 7 days of the
  daily budget. HubSpot contacts, MQLs and SQLs are never sent, so the values already on a result stay. A week inside
  the window that Google no longer reports loses its synced result (results from other sources stay). A linked
  campaign in another currency than its account gets no results (a warning in the summary).
- Spend and outcomes: services/ads_service.sync_initiatives mirrors each result's spend into a Google Ads spend line
  and the results into the campaign's ADS outcomes, as for any other ad result.
- Every write, its audit entries (no actor: Google Ads made the change) and the run summary are saved in one
  transaction, under an advisory lock, so two servers never sync at the same time. The run state is kept in
  integration_sync_state "google_ads" (live) or "google_ads_mock" (mock).
"""

import asyncio
import datetime
import json
import logging
import os
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from postgresql_db.database import get_pool
from services import ads_service
from services import oauth_connection_service as oauth_svc
from services.google_ads_client import (
    MOCK_MODE,
    GoogleAdsApiError,
    connection_mode,
    fetch_campaigns,
    google_ads_mode,
)
from services.google_ads_mock_data import mock_account_campaigns
from services.initiative_service import AUDIT_ENTITY as INITIATIVE_AUDIT_ENTITY

logger = logging.getLogger(__name__)

PROVIDER = "GOOGLE_ADS"
SYNC_NAME = "google_ads"
# Mock mode keeps its own run state, so a mock run never changes what the live sync remembers
MOCK_SYNC_NAME = "google_ads_mock"
RESULT_SOURCE = "GOOGLE_ADS_API"
LINK_SYSTEM = "GOOGLE_ADS"
CHANNEL = "PAID_SEARCH"
SUB_CHANNEL = "GOOGLE_ADS"
DEFAULT_BRAND = "TBD_ALL"
DEFAULT_LOOKBACK_DAYS = 90
MAX_LOOKBACK_DAYS = 730
DEFAULT_SYNC_INTERVAL_MINUTES = 360
MIN_SYNC_INTERVAL_MINUTES = 5
ALREADY_RUNNING_MESSAGE = "A Google Ads sync is already running. Try again in a minute."
NEEDS_SIGN_IN_MESSAGE = "The Google Ads connection needs to be signed in again on OAuth Integrations."
# Campaign types that are about being seen rather than leads
AWARENESS_CHANNEL_TYPES = {"DISPLAY", "VIDEO", "DEMAND_GEN", "DISCOVERY"}

# One lock for both modes: they write the same kind of rows
_SYNC_LOCK_KEY = f"integration_sync_state:{SYNC_NAME}"
_run_lock = asyncio.Lock()


class GoogleAdsSyncAlreadyRunning(Exception):
    """Another Google Ads sync is running (here or on another server). The message is safe to show to users."""

    def __init__(self, message: str = ALREADY_RUNNING_MESSAGE):
        super().__init__(message)


class GoogleAdsSyncFailed(Exception):
    """No Google Ads account could be read. The message is safe to show to users."""


# --- Settings ---

def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_setting(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def google_ads_sync_enabled() -> bool:
    return _flag("GOOGLE_ADS_SYNC_ENABLED", False)


def google_ads_create_campaigns() -> bool:
    return _flag("GOOGLE_ADS_CREATE_CAMPAIGNS", True)


def lookback_days() -> int:
    return _int_setting("GOOGLE_ADS_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS, 7, MAX_LOOKBACK_DAYS)


def sync_interval_minutes() -> int:
    return _int_setting("GOOGLE_ADS_SYNC_INTERVAL_MINUTES", DEFAULT_SYNC_INTERVAL_MINUTES, MIN_SYNC_INTERVAL_MINUTES, 10_080)


def sync_name() -> str:
    return MOCK_SYNC_NAME if google_ads_mode() == MOCK_MODE else SYNC_NAME


# --- Rules (pure, so they're easy to test) ---

def read_window(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    """From the Monday of the week GOOGLE_ADS_LOOKBACK_DAYS ago to today, so the first week is a whole week."""
    start = today - datetime.timedelta(days=lookback_days() - 1)
    return start - datetime.timedelta(days=start.weekday()), today


def initiative_status(google_status: str, start: Optional[datetime.date], end: Optional[datetime.date],
                      today: datetime.date) -> str:
    """A campaign's portal status from its Google status and dates."""
    if google_status == "REMOVED":
        return "CANCELLED"
    if google_status == "PAUSED":
        return "PAUSED"
    if start is not None and start > today:
        return "PLANNED"
    if end is not None and end < today:
        return "COMPLETED"
    return "ACTIVE"


def result_status(status: str) -> str:
    """An ad result's status (ACTIVE, PAUSED, ENDED) from its campaign's portal status."""
    if status == "PAUSED":
        return "PAUSED"
    if status in ("COMPLETED", "CANCELLED"):
        return "ENDED"
    return "ACTIVE"


def objective_for(channel_type: str) -> str:
    return "AWARENESS" if channel_type in AWARENESS_CHANNEL_TYPES else "LEADS"


def campaign_budget(campaign: Dict[str, Any]) -> Optional[Decimal]:
    """The campaign's whole budget: its total budget, else its daily budget times its days when it has an end date."""
    if campaign.get("total_budget") is not None:
        return campaign["total_budget"]
    daily, start, end = campaign.get("daily_budget"), campaign.get("start_date"), campaign.get("end_date")
    if daily is None or start is None or end is None or end < start:
        return None
    return (daily * ((end - start).days + 1)).quantize(Decimal("0.01"))


def week_budget(campaign: Dict[str, Any]) -> Optional[Decimal]:
    daily = campaign.get("daily_budget")
    return (daily * 7).quantize(Decimal("0.01")) if daily is not None else None


# --- Connections ---

def _metadata(raw) -> Dict[str, Any]:
    if isinstance(raw, str):
        try:
            return json.loads(raw) or {}
        except ValueError:
            return {}
    return raw or {}


async def usable_connections(conn, mode: str, connection_ids: Optional[List[uuid.UUID]] = None) -> List[Dict[str, Any]]:
    """CONNECTED Google Ads connections made in `mode`, a company's own first, with their discovered accounts."""
    rows = await conn.fetch(
        """
        SELECT oc.id, oc.company_id, oc.access_token_encrypted, oc.connection_metadata, oc.updated_at,
               comp.brand_id, comp.name AS company_name
        FROM oauth_connections oc
        LEFT JOIN companies comp ON comp.id = oc.company_id
        WHERE oc.provider = $1 AND oc.status = 'CONNECTED' AND ($2::uuid[] IS NULL OR oc.id = ANY($2::uuid[]))
        ORDER BY (oc.company_id IS NULL), oc.updated_at DESC
        """,
        PROVIDER, connection_ids,
    )
    usable = []
    for r in rows:
        meta = _metadata(r["connection_metadata"])
        if connection_mode(meta, r["access_token_encrypted"]) != mode:
            continue
        usable.append({
            "id": r["id"], "company_id": r["company_id"], "company_name": r["company_name"], "brand_id": r["brand_id"],
            "accounts": [a for a in meta.get("discovered_accounts", []) or [] if isinstance(a, dict) and a.get("account_id")],
        })
    return usable


def accounts_to_read(connections: List[Dict[str, Any]]) -> List[tuple]:
    """(connection, account) for each non-manager account, each account once."""
    seen, out = set(), []
    for c in connections:
        for account in c["accounts"]:
            if account.get("is_manager") or account.get("error") or account["account_id"] in seen:
                continue
            seen.add(account["account_id"])
            out.append((c, account))
    return out


# --- Reading ---

async def _read_account(mode: str, connection: Dict[str, Any], account: Dict[str, Any], date_from: datetime.date,
                        date_to: datetime.date, tokens: Dict[Any, Optional[str]]) -> List[Dict[str, Any]]:
    if mode == MOCK_MODE:
        return mock_account_campaigns(account, date_from, date_to)
    if connection["id"] not in tokens:
        tokens[connection["id"]] = await oauth_svc.get_valid_access_token(connection["id"])
    token = tokens[connection["id"]]
    if not token:
        raise GoogleAdsApiError(NEEDS_SIGN_IN_MESSAGE)
    return await fetch_campaigns(token, account, date_from, date_to)


async def _read_all(mode: str, connections: List[Dict[str, Any]], date_from: datetime.date, date_to: datetime.date,
                    summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every readable account with its campaigns; failures go into the summary."""
    tokens: Dict[Any, Optional[str]] = {}
    read = []
    for connection, account in accounts_to_read(connections):
        run = {"account_id": account["account_id"], "account_name": account.get("account_name"),
               "connection_id": str(connection["id"]), "campaigns": 0, "results": 0, "error": None}
        summary["account_runs"].append(run)
        try:
            campaigns = await _read_account(mode, connection, account, date_from, date_to, tokens)
        except GoogleAdsApiError as exc:
            run["error"] = str(exc)
            continue
        except Exception as exc:
            logger.exception(f"Google Ads account read failed: {type(exc).__name__}", extra={"event": "google_ads_account_failed"})
            run["error"] = f"The account couldn't be read ({type(exc).__name__})."
            continue
        run["campaigns"] = len(campaigns)
        run["results"] = sum(len(c["weeks"]) for c in campaigns)
        read.append({"connection": connection, "account": account, "campaigns": campaigns, "run": run})
    summary["accounts"] = len(summary["account_runs"])
    summary["accounts_failed"] = sum(1 for r in summary["account_runs"] if r["error"])
    return read


# --- Writing ---

def _json_rows(rows: List[Dict[str, Any]]) -> str:
    return json.dumps(rows, default=str)


async def _apply(conn, mode: str, read: List[Dict[str, Any]], date_from: datetime.date, today: datetime.date,
                 summary: Dict[str, Any]) -> None:
    ids = await conn.fetchrow(
        "SELECT (SELECT id FROM channels WHERE code = $1) AS channel, (SELECT id FROM sub_channels WHERE code = $2) AS sub_channel, "
        "(SELECT id FROM brands WHERE code = $3) AS brand",
        CHANNEL, SUB_CHANNEL, DEFAULT_BRAND,
    )
    if ids["channel"] is None or ids["sub_channel"] is None or ids["brand"] is None:
        raise RuntimeError("The Paid Search channel, Google Ads sub-channel or TBD/All brand is missing")

    campaign_ids = [c["campaign_id"] for item in read for c in item["campaigns"]]
    linked = {
        r["external_id"]: dict(r)
        for r in await conn.fetch(
            """
            SELECT l.external_id, l.account_id, i.id, i.name, i.status, i.start_date, i.end_date, i.budget_amount,
                   i.currency, i.created_by
            FROM initiative_links l JOIN initiatives i ON i.id = l.initiative_id
            WHERE l.system = $1 AND l.external_id = ANY($2::text[])
            FOR UPDATE OF i
            """,
            LINK_SYSTEM, campaign_ids,
        )
    }

    creates, links, updates, audits, results, synced = [], [], [], [], [], {}
    create_new = google_ads_create_campaigns()
    for item in read:
        account, connection = item["account"], item["connection"]
        currency = (account.get("currency") or "USD").upper()
        for c in item["campaigns"]:
            start, end = c["start_date"], c["end_date"]
            if start and end and end < start:
                end = None
            status = initiative_status(c["status"], start, end, today)
            budget = campaign_budget(c)
            have = linked.get(c["campaign_id"])
            if have is None:
                if not create_new or (c["status"] == "REMOVED" and not c["weeks"]):
                    summary["campaigns_skipped"] += 1
                    continue
                initiative_id = uuid.uuid4()
                details = {"ad_account_id": account["account_id"], "campaign_objective": objective_for(c["channel_type"])}
                description = f"Added by the Google Ads sync from account {account['account_id']}"
                description += " (mock data)." if mode == MOCK_MODE else "."
                row = {
                    "id": str(initiative_id), "name": c["name"][:255], "brand_id": connection["brand_id"] or ids["brand"],
                    "start_date": start, "end_date": end, "budget_amount": budget, "currency": currency,
                    "status": status, "channel_details": details, "description": description,
                }
                creates.append(row)
                links.append({"initiative_id": str(initiative_id), "external_id": c["campaign_id"], "account_id": account["account_id"]})
                audits.append(("CREATE", initiative_id, None, {**{k: v for k, v in row.items() if k != "id"},
                                                               "google_ads_campaign_id": c["campaign_id"], "source": PROVIDER}))
                summary["campaigns_created"] += 1
            else:
                initiative_id = have["id"]
                want = {"status": status, "start_date": start, "end_date": end, "budget_amount": budget}
                if have["created_by"] is None:
                    want["name"] = c["name"][:255]
                changed = {k: v for k, v in want.items() if have[k] != v}
                if changed:
                    updates.append({"id": str(initiative_id), **{k: have[k] for k in ("name", "status", "start_date", "end_date", "budget_amount")}, **changed})
                    audits.append(("UPDATE", initiative_id, {k: have[k] for k in changed}, {**changed, "source": PROVIDER}))
                    summary["campaigns_updated"] += 1
                else:
                    summary["campaigns_unchanged"] += 1
                if have["currency"].strip().upper() != currency:
                    summary["warnings"].append(
                        f"{have['name']} is in {have['currency'].strip()} but its Google Ads account is in {currency}: "
                        "its results weren't synced."
                    )
                    continue
            synced[initiative_id] = set()
            for week, figures in sorted(c["weeks"].items()):
                synced[initiative_id].add(week)
                results.append({
                    "initiative_id": initiative_id, "ad_group": None, "period_start": week,
                    "period_end": week + datetime.timedelta(days=6), "budget_amount": week_budget(c),
                    "spend": figures["spend"], "impressions": figures["impressions"], "clicks": figures["clicks"],
                    "conversions": figures["conversions"], "status": result_status(status),
                })

    if creates:
        await conn.execute(
            """
            INSERT INTO initiatives (id, name, brand_id, channel_id, sub_channel_id, start_date, end_date, budget_amount,
                                     currency, status, data_source, channel_details, description)
            SELECT t.id, t.name, t.brand_id, $2, $3, t.start_date, t.end_date, t.budget_amount, t.currency, t.status,
                   'LINKED', t.channel_details, t.description
            FROM jsonb_to_recordset(CAST($1 AS jsonb)) AS t(
                id uuid, name text, brand_id int, start_date date, end_date date, budget_amount numeric, currency text,
                status text, channel_details jsonb, description text)
            """,
            _json_rows(creates), ids["channel"], ids["sub_channel"],
        )
        await conn.execute(
            """
            INSERT INTO initiative_links (initiative_id, system, external_id, account_id)
            SELECT t.initiative_id, $2, t.external_id, t.account_id
            FROM jsonb_to_recordset(CAST($1 AS jsonb)) AS t(initiative_id uuid, external_id text, account_id text)
            ON CONFLICT (system, external_id) DO NOTHING
            """,
            _json_rows(links), LINK_SYSTEM,
        )
    if updates:
        await conn.execute(
            """
            UPDATE initiatives i
            SET name = t.name, status = t.status, start_date = t.start_date, end_date = t.end_date,
                budget_amount = t.budget_amount, updated_at = now()
            FROM jsonb_to_recordset(CAST($1 AS jsonb)) AS t(
                id uuid, name text, status text, start_date date, end_date date, budget_amount numeric)
            WHERE i.id = t.id
            """,
            _json_rows(updates),
        )
    await ads_service.audit_many(conn, None, INITIATIVE_AUDIT_ENTITY, audits)

    applied = await ads_service.apply_results(conn, results, RESULT_SOURCE, None)
    summary["results_created"] = len(applied["created"])
    summary["results_updated"] = len(applied["updated"])
    summary["results_unchanged"] = applied["unchanged"]

    # Weeks in the window that Google no longer reports: only this sync's own results go
    keys = [(i, w) for i, weeks in synced.items() for w in weeks]
    removed = await conn.fetch(
        """
        DELETE FROM ad_results r
        WHERE r.source = $1 AND r.ad_group IS NULL AND r.initiative_id = ANY($2::uuid[]) AND r.period_start >= $3
          AND NOT EXISTS (
              SELECT 1 FROM unnest($4::uuid[], $5::date[]) AS k(initiative_id, period_start)
              WHERE k.initiative_id = r.initiative_id AND k.period_start = r.period_start)
        RETURNING r.id, r.initiative_id, r.period_start, r.spend, r.impressions, r.clicks, r.conversions
        """,
        RESULT_SOURCE, list(synced), date_from, [k[0] for k in keys], [k[1] for k in keys],
    )
    summary["results_removed"] = len(removed)
    await ads_service.audit_many(conn, None, ads_service.AUDIT_ENTITY, [
        ("DELETE", r["id"], {**{k: r[k] for k in ("initiative_id", "period_start", "spend", "impressions", "clicks", "conversions")},
                             "source": RESULT_SOURCE}, None)
        for r in removed
    ])
    if synced:
        summary["spend_conflicts"] = await ads_service.sync_initiatives(conn, list(synced), None)


def _empty_summary(mode: str, date_from: datetime.date, date_to: datetime.date) -> Dict[str, Any]:
    return {
        "mode": mode, "date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "connections": 0,
        "accounts": 0, "accounts_failed": 0, "campaigns_created": 0, "campaigns_updated": 0, "campaigns_unchanged": 0,
        "campaigns_skipped": 0, "results_created": 0, "results_updated": 0, "results_unchanged": 0,
        "results_removed": 0, "spend_conflicts": 0, "account_runs": [], "warnings": [],
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
            sync_name(), message,
        )


def _partial_error(summary: Dict[str, Any]) -> Optional[str]:
    failed = [r for r in summary["account_runs"] if r["error"]]
    if not failed:
        return None
    first = failed[0]
    others = f" and {len(failed) - 1} more" if len(failed) > 1 else ""
    return f"Couldn't read {first['account_name'] or first['account_id']}{others}: {first['error']}"


async def _run_sync(connection_ids: Optional[List[uuid.UUID]] = None, today: Optional[datetime.date] = None) -> Dict[str, Any]:
    mode = google_ads_mode()
    today = today or datetime.date.today()
    date_from, date_to = read_window(today)
    summary = _empty_summary(mode, date_from, date_to)

    pool = get_pool()
    async with pool.acquire() as conn:
        connections = await usable_connections(conn, mode, connection_ids)
    summary["connections"] = len(connections)

    # Google first, with no database connection held
    read = await _read_all(mode, connections, date_from, date_to, summary)
    if summary["accounts"] and not read:
        raise GoogleAdsSyncFailed(_partial_error(summary) or "No Google Ads account could be read.")

    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval("SELECT pg_try_advisory_xact_lock(hashtext($1))", _SYNC_LOCK_KEY):
                raise GoogleAdsSyncAlreadyRunning()
            await _apply(conn, mode, read, date_from, today, summary)
            await conn.execute(
                """
                INSERT INTO integration_sync_state (name, last_attempt_at, last_success_at, last_error, last_run_summary)
                VALUES ($1, now(), now(), $2, CAST($3 AS jsonb))
                ON CONFLICT (name) DO UPDATE SET last_attempt_at = now(), last_success_at = now(),
                    last_error = EXCLUDED.last_error, last_run_summary = EXCLUDED.last_run_summary
                """,
                sync_name(), _partial_error(summary), json.dumps(summary, default=str),
            )
    return summary


async def sync_google_ads(connection_ids: Optional[List[uuid.UUID]] = None,
                          today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Run one sync (every usable connection, or only these) and return its summary.

    Raises GoogleAdsSyncAlreadyRunning, or GoogleAdsSyncFailed when no account could be read (stored as a failure).
    """
    if _run_lock.locked():
        raise GoogleAdsSyncAlreadyRunning()
    async with _run_lock:
        try:
            summary = await _run_sync(connection_ids, today)
        except GoogleAdsSyncAlreadyRunning:
            raise
        except GoogleAdsSyncFailed as exc:
            logger.warning(f"Google Ads sync could not read any account: {exc}", extra={"event": "google_ads_sync_unavailable"})
            await _record_failure(str(exc))
            raise
        except Exception as exc:
            logger.exception(f"Google Ads sync failed: {type(exc).__name__}: {exc}", extra={"event": "google_ads_sync_failed"})
            await _record_failure(f"The sync stopped with an unexpected error ({type(exc).__name__}). Nothing was changed.")
            raise
    logger.info(
        "Google Ads sync completed",
        extra={"event": "google_ads_sync_completed", "summary": {k: v for k, v in summary.items() if k != "account_runs"}},
    )
    return summary


async def get_google_ads_sync_state() -> Optional[Dict[str, Any]]:
    """The stored state of the Google Ads sync in the current mode, or None before its first run."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_attempt_at, last_success_at, last_error, last_run_summary FROM integration_sync_state WHERE name = $1",
            sync_name(),
        )
    if row is None:
        return None
    state = dict(row)
    if isinstance(state["last_run_summary"], str):
        state["last_run_summary"] = json.loads(state["last_run_summary"])
    return state


async def connected_accounts() -> List[Dict[str, Any]]:
    """The accounts of the usable connections in the current mode (managers included, flagged)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        connections = await usable_connections(conn, google_ads_mode())
    return [
        {"connection_id": str(c["id"]), "account_id": a["account_id"], "account_name": a.get("account_name"),
         "currency": a.get("currency"), "is_manager": bool(a.get("is_manager")), "company_name": c["company_name"]}
        for c in connections for a in c["accounts"]
    ]


async def google_ads_sync_loop(interval_seconds: Optional[int] = None) -> None:
    """Run sync_google_ads() forever on a fixed interval; a failed run is logged and retried next interval."""
    interval = interval_seconds or sync_interval_minutes() * 60
    while True:
        try:
            await sync_google_ads()
        except (GoogleAdsSyncAlreadyRunning, GoogleAdsSyncFailed):
            pass
        except Exception:
            logger.exception("Google Ads sync iteration crashed; will retry next interval",
                             extra={"event": "google_ads_sync_iteration_crashed"})
        await asyncio.sleep(interval)

