"""The campaign and weekly results sync shared by the ad platforms (Google Ads, Microsoft Ads).

Each platform's job (services/ads/google_ads_sync_job.py, services/ads/microsoft_ads_sync_job.py) describes itself with an
AdPlatform (names, settings, how to read one account) and runs through the functions here, so both platforms follow
the same rules:
- Connections: every CONNECTED connection of the platform made in its current mode (mock connections are only read
  in mock mode and real ones only in live mode). Each non-manager account is read once, even when two connections
  list it (a company's own connection wins over the organization-wide one, for the brand).
- Reading, with no database connection held: each account's campaigns and their metrics per week for the platform's
  lookback window (from the Monday of that week to today). An account that can't be read is noted in the summary and
  the others still sync; when no account can be read the run fails.
- Campaigns: matched by initiative_links (the platform's link system, external_id = the campaign id). Without one,
  and when the platform's CREATE_CAMPAIGNS setting is on, a Paid Search campaign is added in the platform's
  sub-channel, named as on the platform, brand of the connection's company (else TBD/All), in the account's currency,
  with the platform's dates and budget (see campaign_budget). A removed campaign with nothing in the window isn't added.
  Linked campaigns follow the platform on every run: status, dates and budget. The name only follows the platform for
  campaigns the sync added (nobody is recorded as creating them).
- Results: one ad result per campaign per week (no ad group), in the platform's own result source, created or updated
  in place by services/ads_service.apply_results, so re-runs never duplicate. HubSpot contacts, MQLs and SQLs are
  never sent, so the values already on a result stay. A week inside the window the platform no longer reports loses
  its synced result (results from other sources stay).
- Every write, its audit entries (no actor) and the run summary are saved in one transaction, under an advisory lock
  per platform, so two servers never sync the same platform at the same time.

Campaigns are read in one shape whatever the platform: campaign_id, name, status (ENABLED, PAUSED or REMOVED),
channel_type, start_date, end_date, daily_budget, total_budget and weeks ({Monday: {spend, impressions, clicks,
conversions}}).
"""

import asyncio
import datetime
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional

from postgresql_db.database import get_pool
from services.shared import ads_service
from services.shared.initiative_service import AUDIT_ENTITY as INITIATIVE_AUDIT_ENTITY

logger = logging.getLogger(__name__)

MOCK_MODE = "mock"
CHANNEL = "PAID_SEARCH"
DEFAULT_BRAND = "TBD_ALL"


class AdSyncAlreadyRunning(Exception):
    """Another sync of the platform is running (here or on another server). The message is safe to show to users."""


class AdSyncFailed(Exception):
    """No account of the platform could be read. The message is safe to show to users."""


@dataclass(frozen=True)
class AdPlatform:
    """What the shared sync needs to know about one ad platform."""

    label: str  # "Google Ads", in messages
    provider: str  # oauth_connections.provider
    sync_name: str  # integration_sync_state name in live mode
    mock_sync_name: str  # ... and in mock mode, so a mock run never changes what the live sync remembers
    result_source: str  # ad_results.source
    link_system: str  # initiative_links.system
    sub_channel: str  # sub_channels.code of the campaigns it adds
    audit_campaign_key: str  # the campaign id's key in a created campaign's audit entry
    event_prefix: str  # log event names
    awareness_channel_types: frozenset  # campaign types that are about being seen rather than leads
    mode: Callable[[], str]
    create_campaigns: Callable[[], bool]
    lookback_days: Callable[[], int]
    # (conn, mode, connection_ids) -> the usable connections; looked up through the platform's module so tests can
    # limit a run to their own connections
    usable_connections: Callable[..., Awaitable[List[Dict[str, Any]]]]
    # (mode, connection, account, date_from, date_to, tokens) -> the account's campaigns with their weeks
    read_account: Callable[..., Awaitable[List[Dict[str, Any]]]]
    api_error: type  # the platform's API error, whose message is safe to show
    already_running: type  # AdSyncAlreadyRunning subclass
    failed: type  # AdSyncFailed subclass
    run_lock: asyncio.Lock = field(default_factory=asyncio.Lock, compare=False)

    def current_sync_name(self) -> str:
        return self.mock_sync_name if self.mode() == MOCK_MODE else self.sync_name

    @property
    def lock_key(self) -> str:
        # One lock for both modes: they write the same kind of rows
        return f"integration_sync_state:{self.sync_name}"


# --- Settings ---

def flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def int_setting(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


# --- Rules (pure, so they're easy to test) ---

def read_window(today: datetime.date, lookback_days: int) -> tuple[datetime.date, datetime.date]:
    """From the Monday of the week `lookback_days` ago to today, so the first week is a whole week."""
    start = today - datetime.timedelta(days=lookback_days - 1)
    return start - datetime.timedelta(days=start.weekday()), today


def initiative_status(platform_status: str, start: Optional[datetime.date], end: Optional[datetime.date],
                      today: datetime.date) -> str:
    """A campaign's portal status from its platform status (ENABLED, PAUSED, REMOVED) and dates."""
    if platform_status == "REMOVED":
        return "CANCELLED"
    if platform_status == "PAUSED":
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


def campaign_budget(campaign: Dict[str, Any], today: datetime.date) -> Optional[Decimal]:
    """The campaign's budget: its total budget, else its daily budget times its days.

    Campaigns usually run without an end date, so for those it's the budget so far: up to today while the campaign is
    running, and up to its last week with spend once it's paused or removed (None when that isn't in the read window:
    the budget already on the portal campaign stays). It grows with every run while the campaign runs.
    """
    if campaign.get("total_budget") is not None:
        return campaign["total_budget"]
    daily, start, end = campaign.get("daily_budget"), campaign.get("start_date"), campaign.get("end_date")
    if daily is None or start is None:
        return None
    if end is None or end < start:
        if campaign.get("status") == "ENABLED":
            end = today
        else:
            spent = [week for week, figures in campaign.get("weeks", {}).items() if figures.get("spend")]
            if not spent:
                return None
            end = min(max(spent) + datetime.timedelta(days=6), today)
        if end < start:
            return Decimal("0.00")
    return (daily * ((end - start).days + 1)).quantize(Decimal("0.01"))


def week_budget(campaign: Dict[str, Any]) -> Optional[Decimal]:
    daily = campaign.get("daily_budget")
    return (daily * 7).quantize(Decimal("0.01")) if daily is not None else None


# --- Connections ---

def metadata(raw) -> Dict[str, Any]:
    if isinstance(raw, str):
        try:
            return json.loads(raw) or {}
        except ValueError:
            return {}
    return raw or {}


async def fetch_usable_connections(conn, provider: str, connection_mode: Callable[[dict, Optional[str]], str],
                                   mode: str, connection_ids: Optional[List[uuid.UUID]] = None) -> List[Dict[str, Any]]:
    """CONNECTED connections of `provider` made in `mode`, a company's own first, with their discovered accounts."""
    rows = await conn.fetch(
        """
        SELECT oc.id, oc.company_id, oc.access_token_encrypted, oc.connection_metadata, oc.updated_at,
               comp.brand_id, comp.name AS company_name
        FROM oauth_connections oc
        LEFT JOIN companies comp ON comp.id = oc.company_id
        WHERE oc.provider = $1 AND oc.status = 'CONNECTED' AND ($2::uuid[] IS NULL OR oc.id = ANY($2::uuid[]))
        ORDER BY (oc.company_id IS NULL), oc.updated_at DESC
        """,
        provider, connection_ids,
    )
    usable = []
    for r in rows:
        meta = metadata(r["connection_metadata"])
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

async def _read_all(platform: AdPlatform, mode: str, connections: List[Dict[str, Any]], date_from: datetime.date,
                    date_to: datetime.date, summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every readable account with its campaigns; failures go into the summary."""
    tokens: Dict[Any, Optional[str]] = {}
    read = []
    for connection, account in accounts_to_read(connections):
        run = {"account_id": account["account_id"], "account_name": account.get("account_name"),
               "connection_id": str(connection["id"]), "campaigns": 0, "results": 0, "error": None}
        summary["account_runs"].append(run)
        try:
            campaigns = await platform.read_account(mode, connection, account, date_from, date_to, tokens)
        except platform.api_error as exc:
            run["error"] = str(exc)
            continue
        except Exception as exc:
            logger.exception(f"{platform.label} account read failed: {type(exc).__name__}",
                             extra={"event": f"{platform.event_prefix}_account_failed"})
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


async def _apply(conn, platform: AdPlatform, mode: str, read: List[Dict[str, Any]], date_from: datetime.date,
                 today: datetime.date, summary: Dict[str, Any]) -> None:
    ids = await conn.fetchrow(
        "SELECT (SELECT id FROM channels WHERE code = $1) AS channel, (SELECT id FROM sub_channels WHERE code = $2) AS sub_channel, "
        "(SELECT id FROM brands WHERE code = $3) AS brand",
        CHANNEL, platform.sub_channel, DEFAULT_BRAND,
    )
    if ids["channel"] is None or ids["sub_channel"] is None or ids["brand"] is None:
        raise RuntimeError(f"The Paid Search channel, {platform.sub_channel} sub-channel or TBD/All brand is missing")

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
            platform.link_system, campaign_ids,
        )
    }

    creates, links, updates, audits, results, synced = [], [], [], [], [], {}
    create_new = platform.create_campaigns()
    for item in read:
        account, connection = item["account"], item["connection"]
        currency = (account.get("currency") or "USD").upper()
        for c in item["campaigns"]:
            start, end = c["start_date"], c["end_date"]
            if start and end and end < start:
                end = None
            status = initiative_status(c["status"], start, end, today)
            budget = campaign_budget(c, today)
            have = linked.get(c["campaign_id"])
            if budget is None and have is not None:
                budget = have["budget_amount"]
            if have is None:
                if not create_new or (c["status"] == "REMOVED" and not c["weeks"]):
                    summary["campaigns_skipped"] += 1
                    continue
                initiative_id = uuid.uuid4()
                objective = "AWARENESS" if c["channel_type"] in platform.awareness_channel_types else "LEADS"
                details = {"ad_account_id": account["account_id"], "campaign_objective": objective}
                description = f"Added by the {platform.label} sync from account {account['account_id']}"
                description += " (mock data)." if mode == MOCK_MODE else "."
                row = {
                    "id": str(initiative_id), "name": c["name"][:255], "brand_id": connection["brand_id"] or ids["brand"],
                    "start_date": start, "end_date": end, "budget_amount": budget, "currency": currency,
                    "status": status, "channel_details": details, "description": description,
                }
                creates.append(row)
                links.append({"initiative_id": str(initiative_id), "external_id": c["campaign_id"], "account_id": account["account_id"]})
                audits.append(("CREATE", initiative_id, None, {**{k: v for k, v in row.items() if k != "id"},
                                                               platform.audit_campaign_key: c["campaign_id"],
                                                               "source": platform.provider}))
                summary["campaigns_created"] += 1
            else:
                initiative_id = have["id"]
                want = {"status": status, "start_date": start, "end_date": end, "budget_amount": budget}
                if have["created_by"] is None:
                    want["name"] = c["name"][:255]
                changed = {k: v for k, v in want.items() if have[k] != v}
                if changed:
                    updates.append({"id": str(initiative_id), **{k: have[k] for k in ("name", "status", "start_date", "end_date", "budget_amount")}, **changed})
                    audits.append(("UPDATE", initiative_id, {k: have[k] for k in changed}, {**changed, "source": platform.provider}))
                    summary["campaigns_updated"] += 1
                else:
                    summary["campaigns_unchanged"] += 1
                if have["currency"].strip().upper() != currency:
                    summary["warnings"].append(
                        f"{have['name']} is in {have['currency'].strip()} but its {platform.label} account is in {currency}: "
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
            _json_rows(links), platform.link_system,
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

    applied = await ads_service.apply_results(conn, results, platform.result_source, None)
    summary["results_created"] = len(applied["created"])
    summary["results_updated"] = len(applied["updated"])
    summary["results_unchanged"] = applied["unchanged"]

    # Weeks in the window that the platform no longer reports: only this sync's own results go
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
        platform.result_source, list(synced), date_from, [k[0] for k in keys], [k[1] for k in keys],
    )
    summary["results_removed"] = len(removed)
    await ads_service.audit_many(conn, None, ads_service.AUDIT_ENTITY, [
        ("DELETE", r["id"], {**{k: r[k] for k in ("initiative_id", "period_start", "spend", "impressions", "clicks", "conversions")},
                             "source": platform.result_source}, None)
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


async def _record_failure(platform: AdPlatform, message: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO integration_sync_state (name, last_attempt_at, last_error)
            VALUES ($1, now(), $2)
            ON CONFLICT (name) DO UPDATE SET last_attempt_at = now(), last_error = EXCLUDED.last_error
            """,
            platform.current_sync_name(), message,
        )


def _partial_error(summary: Dict[str, Any]) -> Optional[str]:
    failed = [r for r in summary["account_runs"] if r["error"]]
    if not failed:
        return None
    first = failed[0]
    others = f" and {len(failed) - 1} more" if len(failed) > 1 else ""
    return f"Couldn't read {first['account_name'] or first['account_id']}{others}: {first['error']}"


async def _run_sync(platform: AdPlatform, connection_ids: Optional[List[uuid.UUID]], today: Optional[datetime.date]) -> Dict[str, Any]:
    mode = platform.mode()
    today = today or datetime.date.today()
    date_from, date_to = read_window(today, platform.lookback_days())
    summary = _empty_summary(mode, date_from, date_to)

    pool = get_pool()
    async with pool.acquire() as conn:
        connections = await platform.usable_connections(conn, mode, connection_ids)
    summary["connections"] = len(connections)

    # The platform first, with no database connection held
    read = await _read_all(platform, mode, connections, date_from, date_to, summary)
    if summary["accounts"] and not read:
        raise platform.failed(_partial_error(summary) or f"No {platform.label} account could be read.")

    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval("SELECT pg_try_advisory_xact_lock(hashtext($1))", platform.lock_key):
                raise platform.already_running()
            await _apply(conn, platform, mode, read, date_from, today, summary)
            await conn.execute(
                """
                INSERT INTO integration_sync_state (name, last_attempt_at, last_success_at, last_error, last_run_summary)
                VALUES ($1, now(), now(), $2, CAST($3 AS jsonb))
                ON CONFLICT (name) DO UPDATE SET last_attempt_at = now(), last_success_at = now(),
                    last_error = EXCLUDED.last_error, last_run_summary = EXCLUDED.last_run_summary
                """,
                platform.current_sync_name(), _partial_error(summary), json.dumps(summary, default=str),
            )
    return summary


async def run(platform: AdPlatform, connection_ids: Optional[List[uuid.UUID]] = None,
              today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Run one sync of the platform (every usable connection, or only these) and return its summary.

    Raises the platform's already_running, or its failed when no account could be read (stored as a failure).
    """
    if platform.run_lock.locked():
        raise platform.already_running()
    async with platform.run_lock:
        try:
            summary = await _run_sync(platform, connection_ids, today)
        except AdSyncAlreadyRunning:
            raise
        except AdSyncFailed as exc:
            logger.warning(f"{platform.label} sync could not read any account: {exc}",
                           extra={"event": f"{platform.event_prefix}_sync_unavailable"})
            await _record_failure(platform, str(exc))
            raise
        except Exception as exc:
            logger.exception(f"{platform.label} sync failed: {type(exc).__name__}: {exc}",
                             extra={"event": f"{platform.event_prefix}_sync_failed"})
            await _record_failure(platform, f"The sync stopped with an unexpected error ({type(exc).__name__}). Nothing was changed.")
            raise
    logger.info(
        f"{platform.label} sync completed",
        extra={"event": f"{platform.event_prefix}_sync_completed",
               "summary": {k: v for k, v in summary.items() if k != "account_runs"}},
    )
    return summary


async def get_sync_state(platform: AdPlatform) -> Optional[Dict[str, Any]]:
    """The stored state of the platform's sync in its current mode, or None before its first run."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_attempt_at, last_success_at, last_error, last_run_summary FROM integration_sync_state WHERE name = $1",
            platform.current_sync_name(),
        )
    if row is None:
        return None
    state = dict(row)
    if isinstance(state["last_run_summary"], str):
        state["last_run_summary"] = json.loads(state["last_run_summary"])
    return state


async def connected_accounts(platform: AdPlatform) -> List[Dict[str, Any]]:
    """The accounts of the usable connections in the current mode (managers included, flagged)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        connections = await platform.usable_connections(conn, platform.mode(), None)
    return [
        {"connection_id": str(c["id"]), "account_id": a["account_id"], "account_name": a.get("account_name"),
         "currency": a.get("currency"), "is_manager": bool(a.get("is_manager")), "company_name": c["company_name"]}
        for c in connections for a in c["accounts"]
    ]


async def sync_loop(platform: AdPlatform, sync: Callable[[], Awaitable[Any]], interval_seconds: int) -> None:
    """Run `sync` forever on a fixed interval; a failed run is logged and retried next interval."""
    while True:
        try:
            await sync()
        except (AdSyncAlreadyRunning, AdSyncFailed):
            pass
        except Exception:
            logger.exception(f"{platform.label} sync iteration crashed; will retry next interval",
                             extra={"event": f"{platform.event_prefix}_sync_iteration_crashed"})
        await asyncio.sleep(interval_seconds)
