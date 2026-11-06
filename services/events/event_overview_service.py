"""Events Overview: the events HubSpot has (its event campaigns), with Navan travel attached where it matches.

Rows:
- A HubSpot event: an event in the portal that is a HubSpot campaign (the HubSpot sync creates one for every event
  campaign), so its campaign is known exactly. Its contact lists, marketing event (through the campaign's assets, else
  by name) and funnel come from HubSpot. Navan trips that match it are merged in: their travel cost and travellers are
  added, and their costs are added to its own.
- A Navan trip with no HubSpot event: an event only Navan has bookings for (a Navan event name, Navan travel cost or
  Navan travellers) that matches no HubSpot event. It shows on its own, with whatever HubSpot results match it.

A Navan trip belongs to a HubSpot event when its HubSpot campaign (picked by hand in event_hubspot_links, else found
from the names by services/events/event_matching) is that event's campaign. Typed-in results (the platform's
"outcomes") fill what HubSpot has nothing for.
"""
import json
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional
from uuid import UUID

from postgresql_db.database import get_pool
from services.events import event_matching
from services.events.event_management_service import InitiativeNotFoundError, list_travellers
from services.events.event_performance_service import EVENTS_CHANNEL, list_event_performance
from services.shared import initiative_metrics as metrics

_NAVAN_SQL = f"""
    SELECT i.id AS initiative_id, i.name AS initiative_name, ed.navan_event_name, ed.travel_budget_amount::float8 AS travel_budget,
           ed.navan_estimate_amount::float8 AS navan_estimate,
           COALESCE((SELECT SUM(s.amount) FROM spend_lines s
                     WHERE s.initiative_id = i.id AND s.source = 'NAVAN' AND s.currency = i.currency), 0)::float8
               AS travel_cost,
           (SELECT count(*) FROM event_travellers t WHERE t.initiative_id = i.id) AS travellers,
           (SELECT COALESCE(SUM(t.bookings), 0) FROM event_travellers t WHERE t.initiative_id = i.id) AS bookings,
           (SELECT MIN(t.first_travel) FROM event_travellers t WHERE t.initiative_id = i.id) AS first_travel,
           (SELECT MAX(t.last_travel) FROM event_travellers t WHERE t.initiative_id = i.id) AS last_travel
    FROM initiatives i
    JOIN channels c ON c.id = i.channel_id AND c.code = '{EVENTS_CHANNEL}'
    LEFT JOIN event_details ed ON ed.initiative_id = i.id
    WHERE ed.navan_event_name IS NOT NULL
       OR EXISTS (SELECT 1 FROM spend_lines s WHERE s.initiative_id = i.id AND s.source = 'NAVAN')
       OR EXISTS (SELECT 1 FROM event_travellers t WHERE t.initiative_id = i.id)
"""
# The events in the portal that are HubSpot campaigns: initiative and campaign
_HUBSPOT_EVENTS_SQL = f"""
    SELECT l.initiative_id, MIN(l.external_id) AS campaign_id
    FROM initiative_links l
    JOIN initiatives i ON i.id = l.initiative_id
    JOIN channels c ON c.id = i.channel_id AND c.code = '{EVENTS_CHANNEL}'
    WHERE l.system = 'HUBSPOT'
    GROUP BY l.initiative_id
"""
# Every initiative that is a HubSpot campaign, in any channel
_CAMPAIGN_INITIATIVES_SQL = "SELECT external_id AS campaign_id, initiative_id FROM initiative_links WHERE system = 'HUBSPOT'"
_CAMPAIGNS_SQL = """
    SELECT id, name, status, currency, budget_total::float8 AS budget_total, spend_total::float8 AS spend_total,
           sessions, new_contacts_first_touch, new_contacts_last_touch, influenced_contacts, list_contacts, funnel,
           assets, start_date
    FROM hubspot_campaigns
"""
_LISTS_SQL = "SELECT id, name, size FROM hubspot_contact_lists WHERE size > 0"
_MARKETING_EVENTS_SQL = """
    SELECT id, name, event_type, status, start_at, end_at, event_url, registrants, attendees, cancellations, no_shows
    FROM hubspot_marketing_events
"""
# HubSpot's account currency, used for a campaign that has none of its own (hs_currency_code empty). HubSpot's budget
# totals report USD for every event campaign with spend; a different one would show in that campaign's own currency.
HUBSPOT_DEFAULT_CURRENCY = "USD"
_COST_CATEGORIES = ("BOOTH_SPACE", "TRAVEL", "GROUND_TRANSPORT", "STAFF_TIME", "MEALS_PER_DIEMS", "MATERIALS", "OTHER")
_RESULT_FIELDS = ("attendees", "leads", "qualified_leads", "demos", "customers")


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def _campaign(row: Dict[str, Any]) -> Dict[str, Any]:
    return {**{k: row[k] for k in (
        "id", "name", "status", "currency", "budget_total", "spend_total", "sessions", "new_contacts_first_touch",
        "new_contacts_last_touch", "influenced_contacts", "list_contacts")},
        "spend_currency": row["currency"] or HUBSPOT_DEFAULT_CURRENCY,
        "funnel": _json(row["funnel"], {})}


def _with_typed_results(turnout: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    """Fill what HubSpot has nothing for (or counts as zero) with the results someone typed in for the event, as the
    platform does everywhere: a synced figure wins, a typed one fills the gap. `typed_fields` says which were filled."""
    typed = {"attended": event.get("attendees"), "leads": event.get("leads"), "mqls": event.get("qualified_leads"),
             "customers": event.get("customers")}
    filled = []
    for field, value in typed.items():
        if value is not None and not turnout.get(field):
            turnout[field] = int(value) if value == int(value) else value
            filled.append(field)
    if "leads" in filled:
        turnout["leads_basis"], turnout["leads_list_name"] = "TYPED", None
    turnout["typed_fields"] = filled
    return turnout


def _named_list(event_names: List[str], campaign: Optional[Dict[str, Any]], assets: Dict[str, Any],
                lists: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A contact list whose name matches the event, when the campaign gives no lists of its own; lists attached to
    the campaign are left out (they're already in its figures)."""
    if campaign and campaign.get("list_contacts"):
        return None
    attached = {str(a.get("id")) for a in (assets.get("OBJECT_LIST") or []) if isinstance(a, dict)} if campaign else set()
    candidates = [(x["id"], x["name"]) for x in lists if x["id"] not in attached]
    for name in event_names:
        found = event_matching.match_list(name, candidates)
        if found:
            return next(x for x in lists if x["id"] == found[0])
    return None


def _leads(campaign: Optional[Dict[str, Any]], named_list: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The event's leads and the funnel among them, from the best source there is (see EventTurnout)."""
    funnel = (campaign or {}).get("funnel") or {}
    if campaign and campaign.get("list_contacts"):
        return {"basis": "CAMPAIGN_LISTS", "leads": campaign["list_contacts"], "funnel": funnel.get("lists") or {}, "list_name": None}
    if named_list:
        return {"basis": "NAMED_LIST", "leads": named_list["size"], "funnel": {}, "list_name": named_list["name"]}
    first_touch = funnel.get("first_touch") or {}
    if campaign and campaign.get("new_contacts_first_touch"):
        return {"basis": "FIRST_TOUCH", "leads": campaign["new_contacts_first_touch"], "funnel": first_touch, "list_name": None}
    return {"basis": None, "leads": None, "funnel": {}, "list_name": None}


def _turnout(campaign: Optional[Dict[str, Any]], marketing_event: Optional[Dict[str, Any]],
             named_list: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    lead = _leads(campaign, named_list)
    stages = lead["funnel"]
    return {
        "registrants": (marketing_event or {}).get("registrants"),
        "attended": (marketing_event or {}).get("attendees"),
        "no_shows": (marketing_event or {}).get("no_shows"),
        "leads": lead["leads"],
        "leads_basis": lead["basis"],
        "leads_list_name": lead["list_name"],
        "new_contacts": (campaign or {}).get("new_contacts_first_touch"),
        "influenced_contacts": (campaign or {}).get("influenced_contacts"),
        "sessions": (campaign or {}).get("sessions"),
        "mqls": stages.get("mql") if stages else None,
        "sqls": stages.get("sql") if stages else None,
        "opportunities": stages.get("opportunity") if stages else None,
        "customers": stages.get("customer") if stages else None,
    }


def _match(event: Dict[str, Any], navan_name: Optional[str], campaigns: Dict[str, Dict[str, Any]],
           marketing_events: Dict[str, Dict[str, Any]], picked: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The event's HubSpot campaign and marketing event: the pick when there is one, else found from the names."""
    if picked is not None:
        campaign = campaigns.get(picked["hubspot_campaign_id"] or "")
        marketing_event = marketing_events.get(picked["hubspot_marketing_event_id"] or "")
        if campaign or marketing_event:
            return {"method": "MANUAL", "picked_none": False, "score": None, "campaign": campaign, "marketing_event": marketing_event}
        return {"method": "NONE", "picked_none": True, "score": None, "campaign": None, "marketing_event": None}
    found: Dict[str, Any] = {"campaign_id": None, "marketing_event_id": None, "score": None}
    for name in dict.fromkeys(n for n in (event["name"], navan_name) if n):
        found = event_matching.match_event(name, list(campaigns.values()), list(marketing_events.values()))
        if found["campaign_id"] or found["marketing_event_id"]:
            break
    campaign = campaigns.get(found["campaign_id"] or "")
    marketing_event = marketing_events.get(found["marketing_event_id"] or "")
    if not (campaign or marketing_event):
        return {"method": "NONE", "picked_none": False, "score": None, "campaign": None, "marketing_event": None}
    return {"method": "AUTO", "picked_none": False, "score": found["score"], "campaign": campaign, "marketing_event": marketing_event}


def _marketing_event_of(campaign_assets: Dict[str, Any], name: str, marketing_events: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A HubSpot event's marketing event: the one among its campaign's assets, else the one its name clearly matches."""
    for asset in campaign_assets.get("MARKETING_EVENT") or []:
        if isinstance(asset, dict) and str(asset.get("id")) in marketing_events:
            return marketing_events[str(asset["id"])]
    found = event_matching.best_match(name, ((e["id"], e["name"]) for e in marketing_events.values()))
    return marketing_events.get(found[0]) if found else None


def _navan_figures(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The Navan figures of the trips attached to an event, added up (none: zeros)."""
    def total(key: str) -> Optional[float]:
        values = [p[key] for p in parts if p[key] is not None]
        return round(sum(values), 2) if values else None
    firsts = [p["first_travel"] for p in parts if p["first_travel"]]
    lasts = [p["last_travel"] for p in parts if p["last_travel"]]
    return {
        "navan_event_name": ", ".join(p["navan_event_name"] for p in parts if p["navan_event_name"]) or None,
        "travel_cost": round(sum(p["travel_cost"] for p in parts), 2),
        "travel_budget": total("travel_budget"), "navan_estimate": total("navan_estimate"),
        "travellers": sum(p["travellers"] for p in parts), "bookings": sum(p["bookings"] for p in parts),
        "first_travel": min(firsts) if firsts else None, "last_travel": max(lasts) if lasts else None,
    }


def _with_hubspot_cost(event: Dict[str, Any], campaign: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The event with its HubSpot campaign's spend as its cost, and the campaign's budget as its budget, where the
    portal has none of its own. A cost the portal knows (even an estimate) always wins, so nothing is counted twice.
    HubSpot doesn't say what the spend was on, so it goes under Other. Money is never converted: when HubSpot's currency
    isn't the event's, the spend isn't used (the panel still shows it, in HubSpot's currency)."""
    spend, budget = (campaign or {}).get("spend_total"), (campaign or {}).get("budget_total")
    out = {**event, "cost_categories": dict(event["cost_categories"]), "cost_source": None, "budget_source": None}
    if not campaign or campaign.get("spend_currency") != event["currency"]:
        return out
    if spend and not event["actual_cost"] and not event["awaiting_cost"]:
        out["actual_cost"] = round(spend, 2)
        out["cost_categories"]["OTHER"] = round(out["cost_categories"]["OTHER"] + spend, 2)
        out["cost_source"] = "HUBSPOT"
    if budget and event["approved_budget"] is None:
        out["approved_budget"] = round(budget, 2)
        out["budget_source"] = "HUBSPOT"
    if out["cost_source"] or out["budget_source"]:
        out["variance"] = None if out["approved_budget"] is None else round(out["actual_cost"] - out["approved_budget"], 2)
        out["cost_per_lead"] = round(out["actual_cost"] / out["leads"], 2) if out["leads"] else None
        out["roi_percent"] = metrics.roi_percent(out["actual_cost"], out["revenue"])
    return out


def _add_trips(event: Dict[str, Any], trips: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The event with the trips' costs and results added: costs are added up, and a result or budget the event lacks
    is taken from a trip."""
    merged = {**event, "cost_categories": dict(event["cost_categories"])}
    for trip in trips:
        merged["actual_cost"] = round(merged["actual_cost"] + trip["actual_cost"], 2)
        merged["awaiting_cost"] = round(merged["awaiting_cost"] + trip["awaiting_cost"], 2)
        merged["other_currency_lines"] += trip["other_currency_lines"]
        for category in _COST_CATEGORIES:
            merged["cost_categories"][category] = round(merged["cost_categories"][category] + trip["cost_categories"][category], 2)
        if merged["approved_budget"] is None:
            merged["approved_budget"] = trip["approved_budget"]
        for field in (*_RESULT_FIELDS, "revenue"):
            if merged.get(field) is None:
                merged[field] = trip.get(field)
    budget = merged["approved_budget"]
    merged["variance"] = None if budget is None else round(merged["actual_cost"] - budget, 2)
    merged["cost_per_lead"] = round(merged["actual_cost"] / merged["leads"], 2) if merged["leads"] else None
    merged["roi_percent"] = metrics.roi_percent(merged["actual_cost"], merged["revenue"])
    return merged


def _aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Totals per currency, months, the cost split and the HubSpot summary of the events shown."""
    by_currency: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_currency[r["currency"]].append(r)

    def total(values: List[Optional[float]]) -> Optional[float]:
        present = [v for v in values if v is not None]
        return round(sum(present), 2) if present else None

    def whole(values: List[Optional[int]]) -> Optional[int]:
        present = [v for v in values if v is not None]
        return sum(present) if present else None

    totals, monthly, breakdown = [], [], []
    for currency, group in by_currency.items():
        budget, actual = total([r["approved_budget"] for r in group]), total([r["actual_cost"] for r in group])
        leads = whole([r["turnout"]["leads"] for r in group])
        # Cost per lead only over the events that have both: a cost with no leads, or leads with no cost, says nothing
        costed = [r for r in group if r["turnout"]["leads"] and r["actual_cost"]]
        costed_leads = sum(r["turnout"]["leads"] for r in costed)
        led_cost = sum(r["actual_cost"] for r in costed)
        totals.append({
            "currency": currency, "event_count": len(group), "approved_budget": budget, "actual_spend": actual,
            "budget_variance": round(budget - actual, 2) if budget is not None and actual is not None else None,
            "travel_cost": round(sum(r["navan"]["travel_cost"] for r in group), 2),
            "leads": leads,
            "cost_per_lead": round(led_cost / costed_leads, 2) if led_cost and costed_leads else None,
        })
        months: Dict[date, Dict[str, float]] = defaultdict(lambda: {"approved_budget": 0, "actual_spend": 0, "travel_cost": 0})
        for r in group:
            m = months[r["start_date"].replace(day=1)]
            m["approved_budget"] += r["approved_budget"] or 0
            m["actual_spend"] += r["actual_cost"] or 0
            m["travel_cost"] += r["navan"]["travel_cost"]
        monthly += [{"month": month, "currency": currency, **{k: round(v, 2) for k, v in figures.items()}}
                    for month, figures in sorted(months.items())]
        breakdown.append({"currency": currency, **{c: round(sum(r["cost_categories"][c] for r in group), 2) for c in _COST_CATEGORIES}})
    totals.sort(key=lambda t: (-t["event_count"], t["currency"]))
    matched = [r for r in rows if r["hubspot"]["method"] != "NONE"]
    turnouts = [r["turnout"] for r in rows]
    return {
        "totals": totals, "monthly": monthly, "cost_breakdown": breakdown,
        "hubspot": {
            "matched": len(matched), "unmatched": len(rows) - len(matched),
            **{k: whole([t[k] for t in turnouts]) for k in (
                "registrants", "attended", "leads", "new_contacts", "influenced_contacts", "mqls", "sqls", "opportunities", "customers")},
            "travellers": sum(r["navan"]["travellers"] for r in rows),
        },
    }


async def list_events_overview(
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    brand_id: int | None = None,
    market_id: int | None = None,
    event_type_id: int | None = None,
    owner_user_id: UUID | None = None,
    status: str | None = None,
    include_trips: bool = False,
) -> Dict[str, Any]:
    """The HubSpot events whose dates overlap [date_from, date_to] (all when open), with the Navan trips joined to
    them, newest first. Navan trips that match no HubSpot event are left out unless `include_trips` (only the trip
    picker asks for them)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        navan = {r["initiative_id"]: dict(r) for r in await conn.fetch(_NAVAN_SQL)}
        hubspot_events = {r["initiative_id"]: r["campaign_id"] for r in await conn.fetch(_HUBSPOT_EVENTS_SQL)}
        campaign_initiatives = {r["campaign_id"]: r["initiative_id"] for r in await conn.fetch(_CAMPAIGN_INITIATIVES_SQL)}
        campaigns = {r["id"]: _campaign(dict(r)) for r in await conn.fetch(_CAMPAIGNS_SQL)}
        assets = {r["id"]: _json(r["assets"], {}) for r in await conn.fetch("SELECT id, assets FROM hubspot_campaigns")}
        # The lists table comes with a later migration: until it's there, events just have no list to match
        has_lists = await conn.fetchval("SELECT to_regclass('public.hubspot_contact_lists') IS NOT NULL")
        contact_lists = [dict(r) for r in await conn.fetch(_LISTS_SQL)] if has_lists else []
        marketing_events = {r["id"]: dict(r) for r in await conn.fetch(_MARKETING_EVENTS_SQL)}
        picks = {r["initiative_id"]: dict(r) for r in await conn.fetch("SELECT * FROM event_hubspot_links")}
    matchable = {cid: {**c, "assets": assets.get(cid)} for cid, c in campaigns.items()}

    # A Navan trip is evidence that its HubSpot campaign is an event. The sync files a campaign under Events by words in
    # its name, so one like "DS - NexusCon 2026" lands under Other: it joins the events when a trip matches it.
    extra: Dict[UUID, str] = {}
    for initiative_id, figures in navan.items():
        if initiative_id in hubspot_events:
            continue
        found = _match({"name": figures["initiative_name"]}, figures["navan_event_name"], matchable, marketing_events, picks.get(initiative_id))
        target = campaign_initiatives.get(found["campaign"]["id"]) if found["campaign"] else None
        if target and target != initiative_id and target not in hubspot_events:
            extra[target] = found["campaign"]["id"]
    performance = await list_event_performance(
        date_from=date_from, date_to=date_to, brand_id=brand_id, market_id=market_id, event_type_id=event_type_id,
        owner_user_id=owner_user_id, status=status, include_ids=list(extra),
    )
    hubspot_events = {**hubspot_events, **extra}
    events = {e["id"]: e for e in performance["events"]}
    event_of_campaign = {campaign_id: initiative_id for initiative_id, campaign_id in hubspot_events.items()}

    # Each Navan trip's HubSpot match; a trip whose campaign is a HubSpot event in the list joins that event
    trips: Dict[UUID, Dict[str, Any]] = {}
    attached: Dict[UUID, List[Dict[str, Any]]] = defaultdict(list)
    for initiative_id, figures in navan.items():
        event = events.get(initiative_id)
        if event is None or initiative_id in hubspot_events:
            continue
        hubspot = _match(event, figures["navan_event_name"], matchable, marketing_events, picks.get(initiative_id))
        trips[initiative_id] = hubspot
        campaign = hubspot["campaign"]
        target = event_of_campaign.get(campaign["id"]) if campaign else None
        if target in events and events[target]["currency"] == event["currency"]:
            attached[target].append({"id": initiative_id, "method": hubspot["method"]})

    def finish(event: Dict[str, Any], hubspot: Dict[str, Any], parts: List[Dict[str, Any]], source: str,
               navan_events: List[Dict[str, Any]]) -> Dict[str, Any]:
        campaign, marketing_event = hubspot["campaign"], hubspot["marketing_event"]
        hubspot["campaign"] = campaigns.get(campaign["id"]) if campaign else None
        names = list(dict.fromkeys(n for n in (event["name"], *(p["navan_event_name"] for p in parts)) if n))
        named_list = _named_list(names, hubspot["campaign"], assets.get(campaign["id"] if campaign else "", {}), contact_lists)
        turnout = _with_typed_results(_turnout(hubspot["campaign"], marketing_event, named_list), event)
        leads = turnout["leads"]
        return {
            **{"cost_source": None, "budget_source": None}, **event, "source": source, "navan_events": navan_events, "navan": _navan_figures(parts), "hubspot": hubspot,
            "turnout": turnout,
            "hubspot_cost_per_lead": round(event["actual_cost"] / leads, 2) if event["actual_cost"] and leads else None,
        }

    rows: List[Dict[str, Any]] = []
    # HubSpot events: their own campaign, and the Navan trips that joined them
    for initiative_id, campaign_id in hubspot_events.items():
        event = events.get(initiative_id)
        if event is None:
            continue
        joined = attached.get(initiative_id, [])
        own = [navan[initiative_id]] if initiative_id in navan else []
        campaign = matchable.get(campaign_id)
        # HubSpot's spend first (when the portal has no cost), then the Navan trips' costs on top: different costs
        merged = _add_trips(_with_hubspot_cost(event, campaign), [events[j["id"]] for j in joined])
        hubspot = {
            "method": "CAMPAIGN", "picked_none": False, "score": None, "campaign": campaign,
            "marketing_event": _marketing_event_of(assets.get(campaign_id, {}), event["name"], marketing_events) if campaign else None,
        }
        rows.append(finish(
            merged, hubspot, own + [navan[j["id"]] for j in joined], "HUBSPOT",
            [{"id": j["id"], "name": events[j["id"]]["name"], "method": j["method"]} for j in joined],
        ))
    # Navan trips that joined no HubSpot event
    joined_ids = {j["id"] for group in attached.values() for j in group}
    for initiative_id, hubspot in trips.items() if include_trips else ():
        if initiative_id not in joined_ids:
            rows.append(finish(events[initiative_id], hubspot, [navan[initiative_id]], "NAVAN", []))

    rows.sort(key=lambda r: (r["start_date"], r["name"]), reverse=True)
    return {"period": performance["period"], "events": rows, **_aggregate(rows)}


async def get_event_overview(initiative_id: UUID) -> Dict[str, Any]:
    """One event with everything above, its travellers, the marketing event's participants and the campaign's assets."""
    overview = await list_events_overview()
    row = next((e for e in overview["events"] if e["id"] == initiative_id), None)
    if row is None:
        raise InitiativeNotFoundError
    travellers = list(await list_travellers(initiative_id))
    for trip in row["navan_events"]:
        travellers += await list_travellers(trip["id"])
    marketing_event_id = (row["hubspot"]["marketing_event"] or {}).get("id")
    campaign_id = (row["hubspot"]["campaign"] or {}).get("id")
    pool = get_pool()
    async with pool.acquire() as conn:
        participants = []
        if marketing_event_id:
            stored = await conn.fetchval("SELECT participants FROM hubspot_marketing_events WHERE id = $1", marketing_event_id)
            participants = _json(stored, []) or []
        campaign_assets = {}
        if campaign_id:
            campaign_assets = _json(await conn.fetchval("SELECT assets FROM hubspot_campaigns WHERE id = $1", campaign_id), {})
    return {**row, "travellers": travellers, "participants": participants, "campaign_assets": campaign_assets}


async def list_hubspot_options() -> Dict[str, List[Dict[str, Any]]]:
    """Every HubSpot campaign and marketing event, newest first, for picking an event's match."""
    pool = get_pool()
    async with pool.acquire() as conn:
        campaigns = await conn.fetch("SELECT id, name, start_date AS starts_on FROM hubspot_campaigns ORDER BY start_date DESC NULLS LAST, lower(name)")
        events = await conn.fetch("SELECT id, name, start_at::date AS starts_on FROM hubspot_marketing_events ORDER BY start_at DESC NULLS LAST, lower(name)")
    return {"campaigns": [dict(r) for r in campaigns], "marketing_events": [dict(r) for r in events]}


async def list_navan_trip_options() -> List[Dict[str, Any]]:
    """The Navan trips that aren't joined to a HubSpot event, newest first, for joining one to an event."""
    overview = await list_events_overview(include_trips=True)
    return [{"id": e["id"], "name": e["name"], "starts_on": e["start_date"]} for e in overview["events"] if e["source"] == "NAVAN"]


async def set_hubspot_link(initiative_id: UUID, campaign_id: Optional[str], marketing_event_id: Optional[str],
                           none: bool, actor_id: UUID) -> None:
    """Save the pick: both ids, one of them, "no match" (none), or neither to go back to the name match."""
    pool = get_pool()
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM initiatives WHERE id = $1)", initiative_id):
            raise InitiativeNotFoundError
        if campaign_id and not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM hubspot_campaigns WHERE id = $1)", campaign_id):
            raise ValueError("That HubSpot campaign isn't there (the HubSpot sync may have removed it).")
        if marketing_event_id and not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM hubspot_marketing_events WHERE id = $1)", marketing_event_id):
            raise ValueError("That HubSpot marketing event isn't there (the HubSpot sync may have removed it).")
        if not (campaign_id or marketing_event_id or none):
            await conn.execute("DELETE FROM event_hubspot_links WHERE initiative_id = $1", initiative_id)
            return
        await conn.execute(
            """
            INSERT INTO event_hubspot_links (initiative_id, hubspot_campaign_id, hubspot_marketing_event_id, updated_by)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (initiative_id) DO UPDATE SET hubspot_campaign_id = EXCLUDED.hubspot_campaign_id,
                hubspot_marketing_event_id = EXCLUDED.hubspot_marketing_event_id, updated_at = now(),
                updated_by = EXCLUDED.updated_by
            """,
            initiative_id, campaign_id or None, marketing_event_id or None, actor_id,
        )


async def set_navan_trip_link(hubspot_event_id: UUID, trip_id: UUID, link: bool, actor_id: UUID) -> None:
    """Join a Navan trip to a HubSpot event (the trip then counts as that event's travel), or take it off again.

    The pick is saved on the trip: its HubSpot campaign becomes the event's. Taking it off saves "no match", so the
    name match doesn't join it again."""
    pool = get_pool()
    async with pool.acquire() as conn:
        campaign_id = await conn.fetchval(
            "SELECT MIN(external_id) FROM initiative_links WHERE system = 'HUBSPOT' AND initiative_id = $1", hubspot_event_id,
        )
        if campaign_id is None:
            raise ValueError("That event isn't a HubSpot campaign, so a Navan trip can't be joined to it.")
        if hubspot_event_id == trip_id or not await conn.fetchval(
            f"SELECT EXISTS (SELECT 1 FROM initiatives i JOIN channels c ON c.id = i.channel_id AND c.code = '{EVENTS_CHANNEL}' WHERE i.id = $1)",
            trip_id,
        ):
            raise InitiativeNotFoundError
        await conn.execute(
            """
            INSERT INTO event_hubspot_links (initiative_id, hubspot_campaign_id, hubspot_marketing_event_id, updated_by)
            VALUES ($1, $2, NULL, $3)
            ON CONFLICT (initiative_id) DO UPDATE SET hubspot_campaign_id = EXCLUDED.hubspot_campaign_id,
                hubspot_marketing_event_id = NULL, updated_at = now(), updated_by = EXCLUDED.updated_by
            """,
            trip_id, campaign_id if link else None, actor_id,
        )
