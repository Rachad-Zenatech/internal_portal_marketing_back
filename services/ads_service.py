"""Paid ads (Google Ads and Bing Ads): results per campaign or ad group per period.

A campaign is an initiative in Paid Search with the Google Ads or Bing Ads sub-channel. Its results replace
Marketing's "SaaS PPC Campaigns" sheet. CTR, CPC, CPL and the other ratios are calculated here, never stored.

After every change to an initiative's results, sync_initiatives() keeps two things in step, in the same transaction:
- Spend: one spend line per result with spend (source GOOGLE_ADS or MICROSOFT_ADS, external_ref "ad_result:<id>",
  category Media / ads, Invoiced), so the cost sheet and the monthly budgets count it. Lines Finance reconciled are
  never changed; they are counted as conflicts.
- Results: monthly outcomes with source ADS (impressions, clicks, conversions, leads, MQLs, SQLs), summed over the
  initiative's results, so the Performance page counts them. Typed-in outcomes stay; synced ones win.

apply_results() upserts by campaign + ad group + period start. The spreadsheet upload uses it, and so will a
future Data Studio feed (source DATA_STUDIO).
"""
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from uuid import UUID

import asyncpg

from postgresql_db.database import get_pool
from services.rbac_service import log_audit
from services.spend_service import AUDIT_ENTITY as SPEND_AUDIT_ENTITY
from services.spend_service import InitiativeNotFoundError

AUDIT_ENTITY = "AdResult"
PLATFORMS = ("GOOGLE_ADS", "BING_ADS")
PLATFORM_LABELS = {"GOOGLE_ADS": "Google Ads", "BING_ADS": "Bing Ads"}
SPEND_SOURCES = {"GOOGLE_ADS": "GOOGLE_ADS", "BING_ADS": "MICROSOFT_ADS"}
VENDORS = {"GOOGLE_ADS": "Google Ads", "BING_ADS": "Microsoft Advertising"}
SPEND_CATEGORY = "MEDIA_ADS"
OUTCOME_SOURCE = "ADS"
EXTERNAL_REF_PREFIX = "ad_result:"

NOT_ADS_MESSAGE = "Ad results are for Google Ads and Bing Ads campaigns."
DUPLICATE_MESSAGE = "There's already a result for this campaign and period. Edit that one instead."
DATES_MESSAGE = "The period can't end before it starts."

# Every value a result holds besides its campaign, in a fixed order (keeps the generated SQL safe)
VALUE_COLUMNS = (
    "ad_group", "period_start", "period_end", "budget_amount", "spend", "impressions", "clicks", "conversions",
    "hubspot_contacts", "mqls", "sqls", "status", "report_url", "tracking_detail",
)
# What an import or feed may change on an existing row (the key columns identify it)
FIGURE_COLUMNS = tuple(c for c in VALUE_COLUMNS if c not in ("ad_group", "period_start"))
_MONEY_COLUMNS = ("budget_amount", "spend", "conversions")
_SQL_TYPES = {
    "ad_group": "text", "period_start": "date", "period_end": "date", "budget_amount": "numeric", "spend": "numeric",
    "impressions": "bigint", "clicks": "bigint", "conversions": "numeric", "hubspot_contacts": "int", "mqls": "int",
    "sqls": "int", "status": "text", "report_url": "text", "tracking_detail": "text",
}

_CONSTRAINT_MESSAGES = {
    "ad_results_dates_check": DATES_MESSAGE,
    "ad_results_report_url_check": "The report link must start with https://",
    "ad_results_status_check": "Unknown status.",
    "ad_results_source_check": "Unknown source.",
}

# Initiatives that are Google Ads or Bing Ads campaigns, with their platform as sc.code
CAMPAIGNS_FROM = """
    FROM initiatives i
    JOIN channels ch ON ch.id = i.channel_id AND ch.code = 'PAID_SEARCH'
    JOIN sub_channels sc ON sc.id = i.sub_channel_id AND sc.code IN ('GOOGLE_ADS', 'BING_ADS')
"""

_SELECT = """
    SELECT r.id, r.initiative_id, i.name AS campaign_name, sc.code AS platform, i.brand_id, b.name AS brand_name,
           i.market_id, mk.name AS market_name, i.currency, r.ad_group, r.period_start, r.period_end,
           r.budget_amount, r.spend, r.impressions, r.clicks, r.conversions, r.hubspot_contacts, r.mqls, r.sqls,
           r.status, r.report_url, r.tracking_detail, r.source, r.updated_at, u.full_name AS updated_by_name
    FROM ad_results r
    JOIN initiatives i ON i.id = r.initiative_id
    JOIN channels ch ON ch.id = i.channel_id AND ch.code = 'PAID_SEARCH'
    JOIN sub_channels sc ON sc.id = i.sub_channel_id AND sc.code IN ('GOOGLE_ADS', 'BING_ADS')
    JOIN brands b ON b.id = i.brand_id
    LEFT JOIN markets mk ON mk.id = i.market_id
    LEFT JOIN users u ON u.id = r.updated_by
"""
_ORDER = "ORDER BY r.period_start DESC, lower(i.name), r.ad_group NULLS FIRST, r.id"


class AdsValidationError(Exception):
    """A request that breaks an ads rule; the message is safe to show to users."""


class AdResultNotFoundError(Exception):
    """No ad result with the requested id."""


# --- Calculated figures ---

def _number(value):
    if value is None:
        return None
    return float(value) if isinstance(value, Decimal) else value


def _ratio(numerator, denominator, scale: int = 1) -> float | None:
    """numerator / denominator (× scale) to 2 dp; None when either is missing or the divisor is 0."""
    if numerator is None or not denominator:
        return None
    return round(float(numerator) / float(denominator) * scale, 2)


def calculated(figures: dict) -> dict:
    """CTR, CPC, conversion rate, CPL, cost per MQL / SQL and budget used, from the stored figures."""
    spend = figures.get("spend")
    return {
        "ctr": _ratio(figures.get("clicks"), figures.get("impressions"), 100),
        "cpc": _ratio(spend, figures.get("clicks")),
        "conversion_rate": _ratio(figures.get("conversions"), figures.get("clicks"), 100),
        "cpl": _ratio(spend, figures.get("hubspot_contacts")),
        "cost_per_mql": _ratio(spend, figures.get("mqls")),
        "cost_per_sql": _ratio(spend, figures.get("sqls")),
        "budget_used_percent": _ratio(spend, figures.get("budget_amount"), 100),
    }


def _shape(row, spend_conflicts: int = 0) -> dict:
    result = {k: _number(v) for k, v in dict(row).items()}
    for column in _MONEY_COLUMNS:
        if result[column] is not None:
            result[column] = round(result[column], 2)
    result.update(calculated(result))
    result["spend_conflicts"] = spend_conflicts
    return result


_SUMMED = ("budget_amount", "spend", "impressions", "clicks", "conversions", "hubspot_contacts", "mqls", "sqls")


def _totals(results: list[dict]) -> list[dict]:
    """Per platform and currency, then ALL per currency. A figure no row has stays empty."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in results:
        groups[(r["platform"], r["currency"])].append(r)
        groups[("ALL", r["currency"])].append(r)
    order = {"GOOGLE_ADS": 0, "BING_ADS": 1, "ALL": 2}
    totals = []
    for (platform, currency), rows in sorted(groups.items(), key=lambda g: (order[g[0][0]], g[0][1])):
        sums = {}
        for column in _SUMMED:
            values = [r[column] for r in rows if r[column] is not None]
            sums[column] = round(sum(values), 2) if values else None
        totals.append({
            "platform": platform, "currency": currency, **sums, **calculated(sums),
            "campaign_count": len({r["initiative_id"] for r in rows}),
        })
    return totals


# --- Reading ---

async def list_results(
    platform: str | None = None, brand_id: int | None = None, market_id: int | None = None,
    initiative_id: UUID | None = None, date_from: date | None = None, date_to: date | None = None,
    status: str | None = None,
) -> dict:
    """Results matching the filters (a period counts when it overlaps [from, to]), with totals."""
    clauses, args = [], []

    def where(sql: str, value) -> None:
        args.append(value)
        clauses.append(sql.format(f"${len(args)}"))

    if platform:
        where("sc.code = {}", platform)
    if brand_id is not None:
        where("i.brand_id = {}", brand_id)
    if market_id is not None:
        where("i.market_id = {}", market_id)
    if initiative_id is not None:
        where("r.initiative_id = {}", initiative_id)
    if date_to is not None:
        where("r.period_start <= {}", date_to)
    if date_from is not None:
        where("(r.period_end IS NULL OR r.period_end >= {})", date_from)
    if status:
        where("r.status = {}", status)
    sql = f"{_SELECT} {'WHERE ' + ' AND '.join(clauses) if clauses else ''} {_ORDER}"

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    results = [_shape(r) for r in rows]
    return {"results": results, "totals": _totals(results)}


async def list_campaigns(platform: str | None = None) -> list[dict]:
    """Every Google Ads and Bing Ads initiative, for pickers."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT i.id AS initiative_id, i.name, sc.code AS platform, b.name AS brand_name, mk.name AS market_name,
                   i.currency, i.status, count(r.id) AS result_count, max(r.period_start) AS last_period_start
            {CAMPAIGNS_FROM}
            JOIN brands b ON b.id = i.brand_id
            LEFT JOIN markets mk ON mk.id = i.market_id
            LEFT JOIN ad_results r ON r.initiative_id = i.id
            WHERE ($1::text IS NULL OR sc.code = $1)
            GROUP BY i.id, i.name, sc.code, b.name, mk.name, i.currency, i.status
            ORDER BY lower(i.name), sc.code
            """,
            platform,
        )
    return [dict(r) for r in rows]


# --- Keeping spend lines and outcomes in step ---

def _period_label(start: date, end: date | None) -> str:
    def day(d: date) -> str:
        return f"{d.day} {d.strftime('%b %Y')}"

    return f"{day(start)} to {day(end)}" if end else f"from {day(start)}"


def _description(row) -> str:
    parts = [row["campaign_name"]]
    if row["ad_group"]:
        parts.append(row["ad_group"])
    parts.append(_period_label(row["period_start"], row["period_end"]))
    text = " · ".join(parts)
    return text if len(text) <= 255 else text[:254] + "…"


async def audit_many(conn, actor_id: UUID | None, entity_type: str, entries: list[tuple]) -> None:
    """Write many audit entries in one statement: (action, entity id, old value, new value) each."""
    if not entries:
        return
    await conn.execute(
        """
        INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, old_value, new_value, created_at)
        SELECT $1, a.action, $2, a.entity_id, CAST(a.old_value AS jsonb), CAST(a.new_value AS jsonb), now()
        FROM unnest($3::text[], $4::text[], $5::text[], $6::text[]) AS a(action, entity_id, old_value, new_value)
        """,
        actor_id, entity_type,
        [e[0] for e in entries],
        [str(e[1]) for e in entries],
        [json.dumps(e[2], default=str) if e[2] is not None else None for e in entries],
        [json.dumps(e[3], default=str) if e[3] is not None else None for e in entries],
    )


_SYNCED_FIELDS = ("source", "category_id", "description", "vendor", "amount", "currency", "incurred_on", "status")


async def sync_initiatives(conn, initiative_ids, actor_id: UUID | None) -> int:
    """Bring these initiatives' ads spend lines and ADS outcomes in step with their results.

    Run inside the caller's transaction. Returns how many reconciled lines were left as they were (conflicts).
    """
    ids = sorted(set(initiative_ids), key=str)
    if not ids:
        return 0
    category = await conn.fetchrow(
        "SELECT id, default_cost_type FROM spend_categories WHERE code = $1", SPEND_CATEGORY
    )
    if category is None:
        raise RuntimeError(f"Spend category {SPEND_CATEGORY} is missing")

    wanted = await conn.fetch(
        """
        SELECT r.id, r.initiative_id, r.ad_group, r.period_start, r.period_end, r.spend,
               i.name AS campaign_name, i.currency, sc.code AS platform
        FROM ad_results r
        JOIN initiatives i ON i.id = r.initiative_id
        JOIN channels ch ON ch.id = i.channel_id AND ch.code = 'PAID_SEARCH'
        JOIN sub_channels sc ON sc.id = i.sub_channel_id AND sc.code IN ('GOOGLE_ADS', 'BING_ADS')
        WHERE r.initiative_id = ANY($1::uuid[]) AND r.spend > 0
        """,
        ids,
    )
    existing = {
        r["external_ref"]: r
        for r in await conn.fetch(
            f"""
            SELECT id, initiative_id, external_ref, {', '.join(_SYNCED_FIELDS)}, reconciled_month
            FROM spend_lines
            WHERE initiative_id = ANY($1::uuid[]) AND source IN ('GOOGLE_ADS', 'MICROSOFT_ADS')
              AND external_ref LIKE '{EXTERNAL_REF_PREFIX}%'
            FOR UPDATE
            """,
            ids,
        )
    }

    creates, updates, audits = [], [], []
    conflicts = 0
    for row in wanted:
        ref = f"{EXTERNAL_REF_PREFIX}{row['id']}"
        desired = {
            "source": SPEND_SOURCES[row["platform"]],
            "category_id": category["id"],
            "description": _description(row),
            "vendor": VENDORS[row["platform"]],
            "amount": row["spend"],
            "currency": row["currency"],
            "incurred_on": row["period_start"],
            "status": "INVOICED",
        }
        current = existing.pop(ref, None)
        if current is None:
            creates.append({**desired, "initiative_id": row["initiative_id"], "external_ref": ref})
            continue
        changed = {f: v for f, v in desired.items() if current[f] != v}
        if not changed:
            continue
        if current["reconciled_month"] is not None:
            conflicts += 1
            continue
        updates.append({**desired, "id": current["id"]})
        audits.append((
            "UPDATE", current["id"],
            {"initiative_id": current["initiative_id"], "external_ref": ref, **{f: current[f] for f in changed}},
            {"initiative_id": current["initiative_id"], "external_ref": ref, **changed},
        ))

    # Lines left over: their result is gone or has no spend any more
    deletes = []
    for ref, current in existing.items():
        if current["reconciled_month"] is not None:
            conflicts += 1
            continue
        deletes.append(current["id"])
        audits.append((
            "DELETE", current["id"],
            {"initiative_id": current["initiative_id"], "external_ref": ref, **{f: current[f] for f in _SYNCED_FIELDS}},
            None,
        ))

    if creates:
        created = await conn.fetch(
            """
            INSERT INTO spend_lines (initiative_id, category_id, description, vendor, status, amount, currency,
                                     incurred_on, cost_type, source, external_ref, created_by, updated_by)
            SELECT u.initiative_id, u.category_id, u.description, u.vendor, u.status, u.amount, u.currency,
                   u.incurred_on, $1, u.source, u.external_ref, $2, $2
            FROM unnest($3::uuid[], $4::int[], $5::text[], $6::text[], $7::text[], $8::numeric[], $9::text[],
                        $10::date[], $11::text[], $12::text[])
                 AS u(initiative_id, category_id, description, vendor, status, amount, currency, incurred_on,
                      source, external_ref)
            RETURNING id, external_ref
            """,
            category["default_cost_type"], actor_id,
            [c["initiative_id"] for c in creates], [c["category_id"] for c in creates],
            [c["description"] for c in creates], [c["vendor"] for c in creates], [c["status"] for c in creates],
            [c["amount"] for c in creates], [c["currency"] for c in creates], [c["incurred_on"] for c in creates],
            [c["source"] for c in creates], [c["external_ref"] for c in creates],
        )
        ids_by_ref = {r["external_ref"]: r["id"] for r in created}
        for c in creates:
            audits.append(("CREATE", ids_by_ref[c["external_ref"]], None, c))

    if updates:
        await conn.execute(
            """
            UPDATE spend_lines s
            SET source = u.source, category_id = u.category_id, description = u.description, vendor = u.vendor,
                amount = u.amount, currency = u.currency, incurred_on = u.incurred_on, status = u.status,
                updated_at = now(), updated_by = $1
            FROM unnest($2::uuid[], $3::text[], $4::int[], $5::text[], $6::text[], $7::numeric[], $8::text[],
                        $9::date[], $10::text[])
                 AS u(id, source, category_id, description, vendor, amount, currency, incurred_on, status)
            WHERE s.id = u.id
            """,
            actor_id,
            [u["id"] for u in updates], [u["source"] for u in updates], [u["category_id"] for u in updates],
            [u["description"] for u in updates], [u["vendor"] for u in updates], [u["amount"] for u in updates],
            [u["currency"] for u in updates], [u["incurred_on"] for u in updates], [u["status"] for u in updates],
        )

    if deletes:
        await conn.execute("DELETE FROM spend_lines WHERE id = ANY($1::uuid[])", deletes)
    await audit_many(conn, actor_id, SPEND_AUDIT_ENTITY, audits)

    # Results: replace the initiative's ADS outcomes with this month-by-month sum
    await conn.execute(
        "DELETE FROM outcomes WHERE initiative_id = ANY($1::uuid[]) AND source = $2", ids, OUTCOME_SOURCE
    )
    await conn.execute(
        """
        WITH sums AS (
            SELECT r.initiative_id, date_trunc('month', r.period_start)::date AS period_start,
                   SUM(r.impressions)::numeric AS impressions, SUM(r.clicks)::numeric AS clicks,
                   SUM(r.conversions) AS conversions, SUM(r.hubspot_contacts)::numeric AS leads,
                   SUM(r.mqls)::numeric AS mqls, SUM(r.sqls)::numeric AS sqls
            FROM ad_results r
            WHERE r.initiative_id = ANY($1::uuid[])
            GROUP BY 1, 2
        )
        INSERT INTO outcomes (initiative_id, period_start, metric_id, value, source, recorded_by, recorded_at)
        SELECT s.initiative_id, s.period_start, m.id, v.value, $2, $3, now()
        FROM sums s
        CROSS JOIN LATERAL (VALUES ('IMPRESSIONS', s.impressions), ('CLICKS', s.clicks),
                                   ('CONVERSIONS', s.conversions), ('LEADS', s.leads),
                                   ('MQLS', s.mqls), ('SQLS', s.sqls)) AS v(code, value)
        JOIN outcome_metrics m ON m.code = v.code
        WHERE v.value IS NOT NULL
        """,
        ids, OUTCOME_SOURCE, actor_id,
    )
    return conflicts


# --- Upserting many results (the spreadsheet upload, and later the Data Studio feed) ---

def changed_figures(current: dict, incoming: dict) -> dict:
    """The figures that would change on an existing row. A figure the source left empty keeps its value."""
    changed = {}
    for column in FIGURE_COLUMNS:
        value = incoming.get(column)
        if value is None:
            continue
        old = current.get(column)
        if isinstance(value, (int, float, Decimal)) and old is not None and not isinstance(value, bool):
            if Decimal(str(value)) == Decimal(str(old)):
                continue
        elif old == value:
            continue
        changed[column] = value
    return changed


def _audit_row(row: dict) -> dict:
    return {k: (str(v) if isinstance(v, (UUID, date)) else _number(v)) for k, v in row.items()}


async def apply_results(conn, rows: list[dict], source: str, actor_id: UUID | None, import_batch_id: UUID | None = None) -> dict:
    """Create or update results by (initiative, ad group, period start), in a fixed number of statements.

    Each row has initiative_id, ad_group, period_start and any FIGURE_COLUMNS (empty ones keep the stored value).
    Rows that change get this source and import batch. Returns {"created": [ids], "updated": [{"id", "previous"}],
    "unchanged": n}. The caller runs sync_initiatives() afterwards.
    """
    if not rows:
        return {"created": [], "updated": [], "unchanged": 0}
    current_rows = await conn.fetch(
        f"""
        SELECT r.id, r.initiative_id, r.ad_group, r.period_start, r.source, r.import_batch_id,
               {', '.join('r.' + c for c in FIGURE_COLUMNS)}
        FROM ad_results r
        JOIN unnest($1::uuid[], $2::text[], $3::date[]) AS k(initiative_id, ad_group, period_start)
          ON r.initiative_id = k.initiative_id AND COALESCE(r.ad_group, '') = COALESCE(k.ad_group, '')
         AND r.period_start = k.period_start
        FOR UPDATE OF r
        """,
        [r["initiative_id"] for r in rows], [r.get("ad_group") for r in rows], [r["period_start"] for r in rows],
    )
    current = {(c["initiative_id"], c["ad_group"] or "", c["period_start"]): dict(c) for c in current_rows}

    creates, updates, audits = [], [], []
    unchanged = 0
    for row in rows:
        key = (row["initiative_id"], row.get("ad_group") or "", row["period_start"])
        existing = current.get(key)
        if existing is None:
            creates.append({**{c: row.get(c) for c in FIGURE_COLUMNS}, "status": row.get("status") or "ACTIVE",
                            "initiative_id": row["initiative_id"], "ad_group": row.get("ad_group"),
                            "period_start": row["period_start"]})
            # The same key twice in one call: the second updates the first
            current[key] = {**creates[-1], "id": None}
            continue
        if existing.get("id") is None:
            unchanged += 1
            continue
        changed = changed_figures(existing, row)
        merged = {**existing, **changed}
        if merged["period_end"] is not None and merged["period_end"] < merged["period_start"]:
            raise AdsValidationError(DATES_MESSAGE)
        if not changed:
            unchanged += 1
            continue
        key_fields = {k: existing[k] for k in ("initiative_id", "ad_group", "period_start")}
        previous = {c: existing[c] for c in FIGURE_COLUMNS}
        previous.update(source=existing["source"], import_batch_id=existing["import_batch_id"])
        updates.append({"id": existing["id"], **{c: merged[c] for c in FIGURE_COLUMNS}, "previous": previous})
        audits.append((
            "UPDATE", existing["id"],
            _audit_row({**key_fields, **{c: existing[c] for c in changed}}),
            _audit_row({**key_fields, **changed, "source": source}),
        ))

    created_ids = []
    if creates:
        columns = ("initiative_id", *VALUE_COLUMNS)
        types = {"initiative_id": "uuid", **_SQL_TYPES}
        params = ", ".join(f"${n}::{types[c]}[]" for n, c in enumerate(columns, start=4))
        inserted = await conn.fetch(
            f"""
            INSERT INTO ad_results ({', '.join(columns)}, source, import_batch_id, created_by, updated_by)
            SELECT {', '.join('u.' + c for c in columns)}, $1, $2, $3, $3
            FROM unnest({params}) AS u({', '.join(columns)})
            RETURNING id, initiative_id
            """,
            source, import_batch_id, actor_id, *[[c[col] for c in creates] for col in columns],
        )
        created_ids = [r["id"] for r in inserted]
        for new_id, c in zip(created_ids, creates):
            audits.append(("CREATE", new_id, None, _audit_row({**c, "source": source})))

    if updates:
        columns = ("id", *FIGURE_COLUMNS)
        types = {"id": "uuid", **_SQL_TYPES}
        params = ", ".join(f"${n}::{types[c]}[]" for n, c in enumerate(columns, start=4))
        await conn.execute(
            f"""
            UPDATE ad_results r
            SET {', '.join(f'{c} = u.{c}' for c in FIGURE_COLUMNS)},
                source = $1, import_batch_id = $2, updated_at = now(), updated_by = $3
            FROM unnest({params}) AS u({', '.join(columns)})
            WHERE r.id = u.id
            """,
            source, import_batch_id, actor_id, *[[u[col] for u in updates] for col in columns],
        )
    await audit_many(conn, actor_id, AUDIT_ENTITY, audits)
    return {
        "created": created_ids,
        "updated": [{"id": u["id"], "previous": u["previous"]} for u in updates],
        "unchanged": unchanged,
    }


# --- One result at a time ---

async def _ads_initiative(conn, initiative_id: UUID):
    row = await conn.fetchrow(
        """
        SELECT i.id, i.name, ch.code AS channel, sc.code AS sub_channel
        FROM initiatives i JOIN channels ch ON ch.id = i.channel_id LEFT JOIN sub_channels sc ON sc.id = i.sub_channel_id
        WHERE i.id = $1
        """,
        initiative_id,
    )
    if row is None:
        raise InitiativeNotFoundError
    if row["channel"] != "PAID_SEARCH" or row["sub_channel"] not in PLATFORMS:
        raise AdsValidationError(NOT_ADS_MESSAGE)
    return row


def _db_error(exc: asyncpg.PostgresError) -> AdsValidationError:
    if isinstance(exc, asyncpg.UniqueViolationError):
        return AdsValidationError(DUPLICATE_MESSAGE)
    return AdsValidationError(_CONSTRAINT_MESSAGES.get(getattr(exc, "constraint_name", None), "Invalid ad result."))


async def _fetch_one(conn, result_id: UUID, spend_conflicts: int = 0) -> dict:
    row = await conn.fetchrow(f"{_SELECT} WHERE r.id = $1", result_id)
    if row is None:
        raise AdResultNotFoundError
    return _shape(row, spend_conflicts)


async def create_result(data: dict, actor_id: UUID) -> dict:
    """Add one result to a Google Ads or Bing Ads campaign, then sync its spend and outcomes."""
    values = {c: data.get(c) for c in VALUE_COLUMNS}
    values["status"] = values["status"] or "ACTIVE"
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                initiative = await _ads_initiative(conn, data["initiative_id"])
                result_id = await conn.fetchval(
                    f"""
                    INSERT INTO ad_results (initiative_id, {', '.join(VALUE_COLUMNS)}, source, created_by, updated_by)
                    VALUES ($1, {', '.join(f'${n}' for n in range(2, len(VALUE_COLUMNS) + 2))}, 'MANUAL',
                            ${len(VALUE_COLUMNS) + 2}, ${len(VALUE_COLUMNS) + 2})
                    RETURNING id
                    """,
                    initiative["id"], *[values[c] for c in VALUE_COLUMNS], actor_id,
                )
                await log_audit(
                    conn, actor_id, "CREATE", AUDIT_ENTITY, result_id,
                    new_value={"initiative_id": initiative["id"], "campaign_name": initiative["name"], **values},
                )
                conflicts = await sync_initiatives(conn, [initiative["id"]], actor_id)
        except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError) as exc:
            raise _db_error(exc) from exc
        return await _fetch_one(conn, result_id, conflicts)


async def update_result(result_id: UUID, changes: dict, actor_id: UUID) -> dict:
    """Change some of a result's values, then sync its campaign's spend and outcomes."""
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                current = await conn.fetchrow(
                    f"""
                    SELECT r.initiative_id, i.name AS campaign_name, r.source, r.import_batch_id,
                           {', '.join('r.' + c for c in VALUE_COLUMNS)}
                    FROM ad_results r JOIN initiatives i ON i.id = r.initiative_id
                    WHERE r.id = $1 FOR UPDATE OF r
                    """,
                    result_id,
                )
                if current is None:
                    raise AdResultNotFoundError
                current = {k: _number(v) for k, v in dict(current).items()}
                origin = {"source": current.pop("source"), "import_batch_id": current.pop("import_batch_id")}
                changed = {c: changes[c] for c in VALUE_COLUMNS if c in changes and changes[c] != current[c]}
                after = {**current, **changed}
                if after["period_end"] is not None and after["period_end"] < after["period_start"]:
                    raise AdsValidationError(DATES_MESSAGE)
                conflicts = 0
                if changed:
                    columns = [c for c in VALUE_COLUMNS if c in changed]
                    assignments = ", ".join(f"{c} = ${n}" for n, c in enumerate(columns, start=2))
                    # Typed in now: no longer as an upload left it, so undoing that upload leaves it alone
                    await conn.execute(
                        f"UPDATE ad_results SET {assignments}, source = 'MANUAL', import_batch_id = NULL, "
                        f"updated_at = now(), updated_by = ${len(columns) + 2} WHERE id = $1",
                        result_id, *[changed[c] for c in columns], actor_id,
                    )
                    # The key fields too, so the entry still reads well once the result is gone
                    context = {k: current[k] for k in ("initiative_id", "campaign_name", "ad_group", "period_start")}
                    await log_audit(
                        conn, actor_id, "UPDATE", AUDIT_ENTITY, result_id,
                        old_value={**context, **{c: current[c] for c in columns}, **origin},
                        new_value={**{k: after[k] for k in context}, **changed, "source": "MANUAL", "import_batch_id": None},
                    )
                    conflicts = await sync_initiatives(conn, [current["initiative_id"]], actor_id)
        except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError) as exc:
            raise _db_error(exc) from exc
        return await _fetch_one(conn, result_id, conflicts)


async def delete_result(result_id: UUID, actor_id: UUID) -> int:
    """Delete a result and sync its campaign's spend and outcomes. Returns the reconciled-cost conflicts."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                f"""
                SELECT r.initiative_id, i.name AS campaign_name, {', '.join('r.' + c for c in VALUE_COLUMNS)}, r.source
                FROM ad_results r JOIN initiatives i ON i.id = r.initiative_id
                WHERE r.id = $1 FOR UPDATE OF r
                """,
                result_id,
            )
            if current is None:
                raise AdResultNotFoundError
            await conn.execute("DELETE FROM ad_results WHERE id = $1", result_id)
            await log_audit(conn, actor_id, "DELETE", AUDIT_ENTITY, result_id, old_value=dict(current))
            return await sync_initiatives(conn, [current["initiative_id"]], actor_id)
