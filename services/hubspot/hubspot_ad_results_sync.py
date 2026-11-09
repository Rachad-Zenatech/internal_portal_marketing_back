"""Paid Search results from HubSpot, until the Google Ads and Microsoft Ads syncs can be used.

After each HubSpot campaign sync, every Google Ads or Bing Ads initiative linked to a HubSpot campaign gets one ad
result (source HUBSPOT, no ad group) holding the campaign's figures to date: spend, budget (when HubSpot has one), new
contacts (first touch) and the MQLs and SQLs among them. Impressions, clicks and conversions stay empty: HubSpot
doesn't have them.

- It is only for display. Unlike the ad platform syncs it adds no spend lines or outcomes (services/shared/ads_service
  .sync_initiatives isn't run), so HubSpot's typed-in spend never becomes a cost.
- An initiative that has a result from anywhere else (an ad platform sync, a sheet, typed in) is left alone, and loses
  its HubSpot one, so nothing is counted twice.
- Figures are only copied when the initiative's currency is HubSpot's; otherwise they would be wrong.
"""
import datetime
from collections import defaultdict
from typing import Any, Dict, List
from uuid import UUID

from services.shared.initiatives import ads_service

RESULT_SOURCE = "HUBSPOT"
LINK_SYSTEM = "HUBSPOT"

_STATUS = {"paused": "PAUSED", "completed": "ENDED"}


def _sum(values: List[Any]) -> Any:
    """The total of the values that exist; None when none does."""
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _stage(campaign: Dict[str, Any], stage: str) -> int | None:
    first_touch = (campaign.get("funnel") or {}).get("first_touch")
    return None if not first_touch else first_touch.get(stage)


def build_result(initiative: Dict[str, Any], campaigns: List[Dict[str, Any]], today: datetime.date) -> Dict[str, Any] | None:
    """The one result for an initiative from its linked campaigns, or None when there's nothing to show (no start)."""
    starts = [c["start_date"] or (c["hubspot_created_at"].date() if c.get("hubspot_created_at") else None) for c in campaigns]
    starts = [s for s in starts if s is not None]
    start = min(starts) if starts else initiative["start_date"]
    if start is None:
        return None
    statuses = {_STATUS.get((c.get("status") or "").lower(), "ACTIVE") for c in campaigns}
    # Ended only when every campaign is; paused when none is running
    status = "ENDED" if statuses == {"ENDED"} else "PAUSED" if "ACTIVE" not in statuses else "ACTIVE"
    ends = [c["end_date"] for c in campaigns if c.get("end_date")]
    end = max(ends) if ends and status == "ENDED" and len(ends) == len(campaigns) else None
    if end is not None and end < start:
        end = None
    budgets = [c["budget_total"] for c in campaigns if c.get("budget_total")]
    return {
        "initiative_id": initiative["id"], "ad_group": None, "period_start": start, "period_end": end,
        "status": status,
        "budget_amount": _sum(budgets),
        "spend": _sum([c.get("spend_total") for c in campaigns]),
        "hubspot_contacts": _sum([c.get("new_contacts_first_touch") for c in campaigns]),
        "mqls": _sum([_stage(c, "mql") for c in campaigns]),
        "sqls": _sum([_stage(c, "sql") for c in campaigns]),
    }


async def sync_paid_search_results(conn, campaigns: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    """Write the HubSpot result of every linked Google Ads or Bing Ads initiative (inside the sync's transaction)."""
    by_id = {c["id"]: c for c in campaigns}
    linked = await conn.fetch(
        f"""
        SELECT l.external_id, i.id, i.start_date, i.currency::text AS currency
        FROM initiative_links l
        JOIN initiatives i ON i.id = l.initiative_id
        {ads_service.CAMPAIGN_JOINS}
        WHERE l.system = $1 AND l.external_id = ANY($2::text[])
        """,
        LINK_SYSTEM, list(by_id),
    )
    initiatives: Dict[UUID, Dict[str, Any]] = {}
    grouped: Dict[UUID, List[Dict[str, Any]]] = defaultdict(list)
    for row in linked:
        campaign = by_id[row["external_id"]]
        initiatives[row["id"]] = dict(row)
        grouped[row["id"]].append(campaign)

    # Results from anywhere else win: such an initiative gets none from HubSpot
    elsewhere = {
        r["initiative_id"]
        for r in await conn.fetch(
            "SELECT DISTINCT initiative_id FROM ad_results WHERE initiative_id = ANY($1::uuid[]) AND source <> $2",
            list(initiatives), RESULT_SOURCE,
        )
    }
    today = datetime.date.today()
    rows, skipped_currency = [], 0
    for initiative_id, group in grouped.items():
        if initiative_id in elsewhere:
            continue
        initiative = initiatives[initiative_id]
        if any(c.get("currency") and c["currency"] != initiative["currency"] for c in group):
            skipped_currency += 1
            continue
        result = build_result(initiative, group, today)
        if result is not None:
            rows.append(result)

    await ads_service.apply_results(conn, rows, RESULT_SOURCE, None)
    # Drop HubSpot results no longer wanted: an initiative that's unlinked, now has others, or whose start moved
    await conn.execute(
        """
        DELETE FROM ad_results r
        WHERE r.source = $1
          AND NOT EXISTS (
              SELECT 1 FROM unnest($2::uuid[], $3::date[]) AS k(initiative_id, period_start)
              WHERE k.initiative_id = r.initiative_id AND k.period_start = r.period_start
          )
        """,
        RESULT_SOURCE, [r["initiative_id"] for r in rows], [r["period_start"] for r in rows],
    )
    summary["ad_results"] = len(rows)
    summary["ad_results_skipped_currency"] = skipped_currency
