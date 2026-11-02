"""Reads campaigns from HubSpot's Campaigns API (marketing/v3/campaigns) with a private app token.

For each campaign: its properties (name, dates, status, budget and spend totals, owner...), its report (sessions, new
contacts by first and last touch, influenced contacts, to date) and its assets by type (lists, emails, pages, events,
social posts, ads...). Owners are named from HubSpot's owners list.

And the campaign's funnel: how many contacts reached each lifecycle stage (lead, MQL, SQL, opportunity, customer),
counted three ways: the contacts whose first touch converting campaign is this one, those whose last touch is, and
the people on its contact lists (for events, the lead lists). Only the counts are kept; people stay in HubSpot. A
contact counts at the furthest stage it has reached (its current stage, or any stage HubSpot has a "date entered"
for) and at every stage before it, so the funnel only narrows.

What HubSpot doesn't give this token: social posts need the "social" scope, revenue attribution is blocked by
field-level permissions, and ads can only be seen when they're attached to a campaign as assets. Deals aren't
counted: none has its campaign set in HubSpot.

Settings (in .env):
- HUBSPOT_API_KEY: a private app access token ("pat-..."), with marketing.campaigns.read, crm.objects.owners.read,
  and for the funnel crm.objects.contacts.read and crm.lists.read. Leave empty to turn HubSpot off.
"""

import asyncio
import datetime
import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.hubapi.com"
PAGE_SIZE = 100
# Stop paging after this many pages, so a misbehaving answer can't loop forever
MAX_PAGES = 200
TIMEOUT_SECONDS = 30
# Requests in flight at once, and started per second: under HubSpot's private app limit (100 per 10 seconds)
MAX_CONCURRENT = 6
REQUESTS_PER_SECOND = 8
# Tries for one request when HubSpot says slow down (429) or has a passing error (5xx)
MAX_TRIES = 4

CAMPAIGN_PROPERTIES = [
    "hs_name", "hs_start_date", "hs_end_date", "hs_campaign_status", "hs_currency_code",
    "hs_budget_items_sum_amount", "hs_spend_items_sum_amount", "hs_owner", "hs_utm", "hs_notes", "hs_audience",
]

# The asset types read for every campaign, in the order they're shown
ASSET_TYPES = (
    "SOCIAL_BROADCAST", "AD_CAMPAIGN", "MARKETING_EMAIL", "LANDING_PAGE", "SITE_PAGE", "BLOG_POST", "FORM", "CTA",
    "MARKETING_EVENT", "OBJECT_LIST", "SEQUENCE", "EXTERNAL_WEB_URL", "MEETING_EVENT",
)
# Counted only: a meeting's name is the people in it, and people's details stay in HubSpot
COUNT_ONLY_ASSET_TYPES = {"MEETING_EVENT"}

# The funnel's stages, in order
FUNNEL_STAGES = ("lead", "mql", "sql", "opportunity", "customer")
# How far each HubSpot lifecycle stage is along the funnel (1 = lead ... 5 = customer); 0 is before it
_STAGE_RANK = {
    "subscriber": 0, "other": 0, "lead": 1, "1465184333": 1,  # Unqualified Lead
    "marketingqualifiedlead": 2, "salesqualifiedlead": 3, "opportunity": 4, "customer": 5, "evangelist": 5,
}
# The "date entered" property of each funnel stage
_STAGE_ENTERED = {
    "lead": "hs_v2_date_entered_lead", "mql": "hs_v2_date_entered_marketingqualifiedlead",
    "sql": "hs_v2_date_entered_salesqualifiedlead", "opportunity": "hs_v2_date_entered_opportunity",
    "customer": "hs_v2_date_entered_customer",
}
FIRST_TOUCH_PROPERTY = "hs_analytics_first_touch_converting_campaign"
LAST_TOUCH_PROPERTY = "hs_analytics_last_touch_converting_campaign"
_CONTACT_PROPERTIES = ["lifecyclestage", FIRST_TOUCH_PROPERTY, LAST_TOUCH_PROPERTY, *_STAGE_ENTERED.values()]
CONTACTS_OBJECT_TYPE = "0-1"
# Contacts read at once by the batch API
BATCH_SIZE = 100


class HubSpotUnavailable(Exception):
    """HubSpot isn't set up or couldn't be read. The message is safe to show to users."""


def _token() -> str:
    return (os.getenv("HUBSPOT_API_KEY") or "").strip()


def hubspot_configured() -> bool:
    return bool(_token())


FIRST_TOUCH = "FIRST_TOUCH"
LAST_TOUCH = "LAST_TOUCH"


def hubspot_attribution() -> str:
    """Which new contacts count as a campaign's leads: FIRST_TOUCH (default; HUBSPOT_ATTRIBUTION=first) credits the
    campaign that first brought them in, LAST_TOUCH (HUBSPOT_ATTRIBUTION=last) the last one before they converted."""
    return LAST_TOUCH if (os.getenv("HUBSPOT_ATTRIBUTION") or "").strip().lower() in ("last", "last_touch") else FIRST_TOUCH


class _Limit:
    """At most MAX_CONCURRENT requests in flight, started no faster than REQUESTS_PER_SECOND."""

    def __init__(self) -> None:
        self._slots = asyncio.Semaphore(MAX_CONCURRENT)
        self._pace = asyncio.Lock()
        self._next_start = 0.0

    async def __aenter__(self) -> None:
        await self._slots.acquire()
        async with self._pace:
            loop = asyncio.get_running_loop()
            wait = self._next_start - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_start = max(loop.time(), self._next_start) + 1 / REQUESTS_PER_SECOND

    async def __aexit__(self, *exc: Any) -> None:
        self._slots.release()


async def _get(client: httpx.AsyncClient, limit: _Limit, path: str, params: Optional[Dict[str, Any]] = None,
               body: Optional[Dict[str, Any]] = None) -> httpx.Response:
    """GET (or POST, with a body) with the token, waiting and trying again when HubSpot asks to slow down or has a
    passing error."""
    response: Optional[httpx.Response] = None
    for attempt in range(MAX_TRIES):
        async with limit:
            if body is None:
                response = await client.get(f"{API_BASE}{path}", params=params)
            else:
                response = await client.post(f"{API_BASE}{path}", params=params, json=body)
        if response.status_code != 429 and response.status_code < 500:
            return response
        wait = float(response.headers.get("Retry-After") or 0) or 1.5 * (attempt + 1)
        await asyncio.sleep(min(wait, 10))
    return response


def _raise_for(response: httpx.Response, what: str) -> None:
    if response.status_code == 401:
        raise HubSpotUnavailable("HubSpot didn't accept the token. Check HUBSPOT_API_KEY.")
    if response.status_code == 403:
        raise HubSpotUnavailable(f"The HubSpot token isn't allowed to read {what}. Add the scope to the private app.")
    if response.status_code != 200:
        raise HubSpotUnavailable(f"HubSpot didn't return the {what} (status {response.status_code}).")


async def _all_pages(client: httpx.AsyncClient, limit: _Limit, path: str, params: Dict[str, Any], what: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    after: Optional[str] = None
    for _ in range(MAX_PAGES):
        response = await _get(client, limit, path, {**params, "limit": PAGE_SIZE, **({"after": after} if after else {})})
        _raise_for(response, what)
        body = response.json()
        results.extend(r for r in body.get("results") or [] if isinstance(r, dict))
        after = ((body.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break
    return results


async def _owner_names(client: httpx.AsyncClient, limit: _Limit) -> Dict[str, str]:
    """Owner names by HubSpot user id and owner id (a campaign's hs_owner is a user id). Empty if owners can't be read."""
    try:
        owners = await _all_pages(client, limit, "/crm/v3/owners", {}, "owners")
    except HubSpotUnavailable as exc:
        logger.warning(f"HubSpot owners couldn't be read; campaigns are shown without owners: {exc}")
        return {}
    names: Dict[str, str] = {}
    for owner in owners:
        name = " ".join(p for p in (owner.get("firstName"), owner.get("lastName")) if p) or owner.get("email") or ""
        if not name:
            continue
        for key in ("userId", "id"):
            if owner.get(key) is not None:
                names[str(owner[key])] = name
    return names


async def _assets(client: httpx.AsyncClient, limit: _Limit, campaign_id: str, asset_type: str) -> Optional[List[Dict[str, Any]]]:
    """The campaign's assets of one type, or None when HubSpot won't give them (the type stays unknown)."""
    response = await _get(client, limit, f"/marketing/v3/campaigns/{campaign_id}/assets/{asset_type}", {"limit": PAGE_SIZE})
    if response.status_code != 200:
        return None
    items = []
    for item in response.json().get("results") or []:
        if isinstance(item, dict) and item.get("id") is not None:
            items.append({"id": str(item["id"]), "name": item.get("name") or None})
    return items


async def _report(client: httpx.AsyncClient, limit: _Limit, campaign_id: str) -> Dict[str, Any]:
    response = await _get(client, limit, f"/marketing/v3/campaigns/{campaign_id}/reports/metrics")
    return response.json() if response.status_code == 200 else {}


async def _details(client: httpx.AsyncClient, limit: _Limit, campaign_id: str) -> Tuple[Dict[str, Any], Dict[str, Any], int]:
    """The campaign's report and assets (by type), and how many asset types HubSpot wouldn't give."""
    report, *by_type = await asyncio.gather(
        _report(client, limit, campaign_id), *(_assets(client, limit, campaign_id, t) for t in ASSET_TYPES)
    )
    assets: Dict[str, Any] = {}
    unreadable = 0
    for asset_type, items in zip(ASSET_TYPES, by_type):
        if items is None:
            unreadable += 1
        elif items:
            assets[asset_type] = {"count": len(items)} if asset_type in COUNT_ONLY_ASSET_TYPES else items
    return report, assets, unreadable


def stages_reached(contact: Dict[str, Any]) -> List[str]:
    """The funnel stages a contact has reached: up to the furthest of its current stage and any stage it has a
    "date entered" for, with every stage before it (HubSpot can date an opportunity whose SQL stage was skipped)."""
    furthest = max(
        _STAGE_RANK.get(str(contact.get("lifecyclestage") or ""), 0),
        *(n for n, stage in enumerate(FUNNEL_STAGES, start=1) if contact.get(_STAGE_ENTERED[stage])),
        0,
    )
    return list(FUNNEL_STAGES[:furthest])


def _funnel(contacts: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = dict.fromkeys(FUNNEL_STAGES, 0)
    for contact in contacts:
        for stage in stages_reached(contact):
            counts[stage] += 1
    return counts


async def _attributed_contacts(client: httpx.AsyncClient, limit: _Limit) -> List[Dict[str, Any]]:
    """Every contact with a first or last touch converting campaign, with its stage properties."""
    contacts: List[Dict[str, Any]] = []
    after: Optional[str] = None
    for _ in range(MAX_PAGES):
        body: Dict[str, Any] = {
            "filterGroups": [
                {"filters": [{"propertyName": FIRST_TOUCH_PROPERTY, "operator": "HAS_PROPERTY"}]},
                {"filters": [{"propertyName": LAST_TOUCH_PROPERTY, "operator": "HAS_PROPERTY"}]},
            ],
            "properties": _CONTACT_PROPERTIES, "limit": PAGE_SIZE, **({"after": after} if after else {}),
        }
        response = await _get(client, limit, "/crm/v3/objects/contacts/search", body=body)
        _raise_for(response, "contacts")
        page = response.json()
        contacts.extend(r.get("properties") or {} for r in page.get("results") or [])
        after = ((page.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break
    return contacts


async def _list_members(client: httpx.AsyncClient, limit: _Limit, list_id: str) -> Optional[List[str]]:
    """The contact ids on a contact list; None for a list of something else (companies, deals...) or one deleted."""
    response = await _get(client, limit, f"/crm/v3/lists/{list_id}", {"includeFilters": "false"})
    if response.status_code == 404:
        return None
    _raise_for(response, "contact lists")
    if (response.json().get("list") or {}).get("objectTypeId") != CONTACTS_OBJECT_TYPE:
        return None
    members: List[str] = []
    after: Optional[str] = None
    for _ in range(MAX_PAGES):
        response = await _get(client, limit, f"/crm/v3/lists/{list_id}/memberships", {"limit": 250, **({"after": after} if after else {})})
        _raise_for(response, "contact lists")
        page = response.json()
        members.extend(str(r["recordId"]) for r in page.get("results") or [] if r.get("recordId") is not None)
        after = ((page.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break
    return members


async def _contacts_by_id(client: httpx.AsyncClient, limit: _Limit, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """The stage properties of these contacts, by id (contacts deleted since are left out)."""
    async def batch(chunk: List[str]) -> List[Dict[str, Any]]:
        response = await _get(client, limit, "/crm/v3/objects/contacts/batch/read",
                              body={"inputs": [{"id": i} for i in chunk], "properties": _CONTACT_PROPERTIES})
        # 207: some contacts no longer exist; the rest are still returned
        if response.status_code == 207:
            return response.json().get("results") or []
        _raise_for(response, "contacts")
        return response.json().get("results") or []
    chunks = [ids[n:n + BATCH_SIZE] for n in range(0, len(ids), BATCH_SIZE)]
    found = await asyncio.gather(*(batch(chunk) for chunk in chunks))
    return {str(r["id"]): r.get("properties") or {} for results in found for r in results}


async def _funnels(client: httpx.AsyncClient, limit: _Limit, assets_by_campaign: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per campaign id: {"list_contacts": n or None, "funnel": {"first_touch": {...}, "last_touch": {...}, "lists": {...}}}."""
    attributed = await _attributed_contacts(client, limit)
    list_ids = sorted({a["id"] for assets in assets_by_campaign.values() for a in assets.get("OBJECT_LIST") or []})
    members = dict(zip(list_ids, await asyncio.gather(*(_list_members(client, limit, i) for i in list_ids))))
    everyone = sorted({m for found in members.values() if found for m in found})
    people = await _contacts_by_id(client, limit, everyone)

    result: Dict[str, Dict[str, Any]] = {}
    for campaign_id, assets in assets_by_campaign.items():
        lists = [members.get(a["id"]) for a in assets.get("OBJECT_LIST") or []]
        on_lists = {m for found in lists if found for m in found}
        contact_lists = any(found is not None for found in lists)
        result[campaign_id] = {
            "list_contacts": len(on_lists) if contact_lists else None,
            "funnel": {
                "first_touch": _funnel([c for c in attributed if c.get(FIRST_TOUCH_PROPERTY) == campaign_id]),
                "last_touch": _funnel([c for c in attributed if c.get(LAST_TOUCH_PROPERTY) == campaign_id]),
                **({"lists": _funnel([people[m] for m in on_lists if m in people])} if contact_lists else {}),
            },
        }
    return result


def _amount(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01")) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _date(value: Any) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _timestamp(value: Any) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _count(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _text(value: Any, max_length: int) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text[:max_length] or None


def _campaign_row(raw: Dict[str, Any], report: Dict[str, Any], assets: Dict[str, Any], owners: Dict[str, str]) -> Dict[str, Any]:
    props = raw.get("properties") or {}
    start, end = _date(props.get("hs_start_date")), _date(props.get("hs_end_date"))
    if start and end and end < start:
        end = None
    currency = _text(props.get("hs_currency_code"), 3)
    return {
        "id": str(raw["id"]),
        "name": _text(props.get("hs_name"), 500) or f"Campaign {raw['id']}",
        "status": _text(props.get("hs_campaign_status"), 30),
        "start_date": start,
        "end_date": end,
        "currency": currency.upper() if currency else None,
        "budget_total": _amount(props.get("hs_budget_items_sum_amount")),
        "spend_total": _amount(props.get("hs_spend_items_sum_amount")),
        "owner_name": owners.get(str(props.get("hs_owner") or "")),
        "utm": _text(props.get("hs_utm"), 500),
        "notes": _text(props.get("hs_notes"), 5000),
        "audience": _text(props.get("hs_audience"), 5000),
        "sessions": _count(report.get("sessions")),
        "new_contacts_first_touch": _count(report.get("newContactsFirstTouch")),
        "new_contacts_last_touch": _count(report.get("newContactsLastTouch")),
        "influenced_contacts": _count(report.get("influencedContacts")),
        "assets": assets,
        "hubspot_created_at": _timestamp(raw.get("createdAt")),
        "hubspot_updated_at": _timestamp(raw.get("updatedAt")),
    }


async def fetch_hubspot_campaigns() -> Dict[str, Any]:
    """Every campaign with its report, assets and funnel:
    {"campaigns": [...rows...], "unreadable_asset_types": n, "funnel_error": message or None}.
    Raises HubSpotUnavailable when the campaigns themselves can't be read; when only the funnel can't be, the
    campaigns come without it (list_contacts None, funnel {}) and funnel_error says why."""
    if not hubspot_configured():
        raise HubSpotUnavailable("HubSpot isn't set up. Add HUBSPOT_API_KEY to the settings.")
    limit = _Limit()
    headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
    funnel_error: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers=headers) as client:
            campaigns = [c for c in await _all_pages(client, limit, "/marketing/v3/campaigns", {"properties": ",".join(CAMPAIGN_PROPERTIES)}, "campaigns") if c.get("id")]
            owners = await _owner_names(client, limit)
            details = await asyncio.gather(*(_details(client, limit, str(c["id"])) for c in campaigns))
            try:
                funnels = await _funnels(client, limit, {str(c["id"]): d[1] for c, d in zip(campaigns, details)})
            except HubSpotUnavailable as exc:
                logger.warning(f"HubSpot funnel couldn't be read; campaigns are synced without it: {exc}")
                funnels, funnel_error = {}, str(exc)
    except HubSpotUnavailable:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HubSpotUnavailable(f"Couldn't reach HubSpot ({type(exc).__name__}).") from exc

    rows = []
    for raw, (report, assets, _) in zip(campaigns, details):
        row = _campaign_row(raw, report, assets, owners)
        row.update(funnels.get(row["id"]) or {"list_contacts": None, "funnel": {}})
        rows.append(row)
    return {"campaigns": rows, "unreadable_asset_types": sum(d[2] for d in details), "funnel_error": funnel_error}
