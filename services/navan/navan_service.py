"""Reads bookings (flights, hotels, cars) from Navan's Booking Data API.

Navan has no API for its events yet, so an event's travel cost is worked out from its bookings: each booking made
through a Navan event carries the event's name (eventName). Bookings that were cancelled or voided don't count.

Settings (in .env):
- NAVAN_CLIENT_ID / NAVAN_CLIENT_SECRET: an API credential with "Booking: Read Only". Leave empty to turn Navan off.
- NAVAN_TOKEN_URL: defaults to Navan's standard one (EU accounts use app-fra.navan.com).
- NAVAN_API_BASE: defaults to https://api.navan.com
- NAVAN_LOOKBACK_DAYS: how far back to read bookings by creation date (default 365).
- NAVAN_MODE: "live" (default) reads Navan; "demo" makes the sync use generated bookings for the demo events
  instead (services/navan/navan_demo_service.py), without calling Navan.
"""

import datetime
import hashlib
import logging
import os
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_URL = "https://app.navan.com/ta-auth/oauth/token"
DEFAULT_API_BASE = "https://api.navan.com"
DEFAULT_LOOKBACK_DAYS = 365
PAGE_SIZE = 100
# Stop paging after this many pages, so a misbehaving answer can't loop forever (50,000 bookings)
MAX_PAGES = 500
TIMEOUT_SECONDS = 60

# Bookings in these states cost nothing
INACTIVE_STATUSES = {"CANCELED", "CANCELLED", "VOIDED"}

# Booking types kept apart in an event's cost (Navan's bookingType); any other type counts as OTHER
BOOKING_TYPES = ("FLIGHT", "HOTEL", "CAR", "RAIL")
OTHER_BOOKING_TYPE = "OTHER"

LIVE_MODE = "live"
DEMO_MODE = "demo"

# Navan sends country names; events store two-letter codes
COUNTRY_CODES = {
    "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "USA": "US", "CANADA": "CA", "UNITED KINGDOM": "GB",
    "GERMANY": "DE", "AUSTRALIA": "AU", "SOUTH KOREA": "KR", "KOREA": "KR", "KOREA, REPUBLIC OF": "KR",
}


class NavanUnavailable(Exception):
    """Navan isn't set up or couldn't be reached. The message is safe to show to users."""


def _setting(name: str) -> str:
    return (os.getenv(name) or "").strip()


def navan_mode() -> str:
    """"demo" when NAVAN_MODE is demo (generated bookings, Navan isn't called), else "live"."""
    return DEMO_MODE if _setting("NAVAN_MODE").lower() == DEMO_MODE else LIVE_MODE


def booking_type(value: Any) -> str:
    """FLIGHT, HOTEL, CAR, RAIL, or OTHER for anything else."""
    kind = str(value or "").strip().upper()
    return kind if kind in BOOKING_TYPES else OTHER_BOOKING_TYPE


def navan_configured() -> bool:
    return bool(_setting("NAVAN_CLIENT_ID") and _setting("NAVAN_CLIENT_SECRET"))


def _lookback_days() -> int:
    try:
        return max(1, int(_setting("NAVAN_LOOKBACK_DAYS") or DEFAULT_LOOKBACK_DAYS))
    except ValueError:
        return DEFAULT_LOOKBACK_DAYS


async def fetch_navan_bookings() -> List[Dict[str, Any]]:
    """Every booking created in the lookback window. Raises NavanUnavailable on any failure."""
    if not navan_configured():
        raise NavanUnavailable("Navan isn't set up. Add NAVAN_CLIENT_ID and NAVAN_CLIENT_SECRET to the settings.")

    token_url = _setting("NAVAN_TOKEN_URL") or DEFAULT_TOKEN_URL
    api_base = (_setting("NAVAN_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    created_from = now - _lookback_days() * 86400

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            token_response = await client.post(token_url, data={
                "grant_type": "client_credentials",
                "client_id": _setting("NAVAN_CLIENT_ID"),
                "client_secret": _setting("NAVAN_CLIENT_SECRET"),
            })
            if token_response.status_code != 200:
                raise NavanUnavailable(f"Navan didn't accept the API credential (status {token_response.status_code}).")
            headers = {"Authorization": f"Bearer {token_response.json()['access_token']}", "Accept": "application/json"}

            bookings: List[Dict[str, Any]] = []
            for page in range(MAX_PAGES):
                response = await client.get(f"{api_base}/v1/bookings", headers=headers, params={
                    "createdFrom": created_from, "createdTo": now, "page": page, "size": PAGE_SIZE,
                })
                if response.status_code != 200:
                    raise NavanUnavailable(f"Navan didn't return the bookings (status {response.status_code}).")
                body = response.json()
                batch = body.get("data") or []
                bookings.extend(b for b in batch if isinstance(b, dict))
                total_pages = (body.get("page") or {}).get("totalPages")
                if not batch or (total_pages is not None and page + 1 >= total_pages):
                    break
            return bookings
    except NavanUnavailable:
        raise
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise NavanUnavailable(f"Couldn't reach Navan ({type(exc).__name__}).") from exc


def _amount(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _date(value: Any) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _country_code(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    return text if len(text) == 2 and text.isalpha() else COUNTRY_CODES.get(text)


def summarize_events(bookings: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Active booking totals per Navan event, keyed by the lower-cased event name.

    {"daas grand opening": {"name": "DaaS Grand Opening", "bookings": 12, "first_travel": date, "last_travel": date,
                            "city": "Mesa", "country_code": "US", "totals": {"USD": Decimal("7437.52")},
                            "by_type": {("FLIGHT", "USD"): {"amount": Decimal("5365.90"), "bookings": 8}, ...}}}
    totals is everything active per currency; by_type splits it by booking type (services.navan.navan_service.booking_type)
    and currency. The city is where most of the event's hotel nights are (else its flights land). Events whose bookings are all
    cancelled are kept with no totals, so their cost is removed.
    """
    events: Dict[str, Dict[str, Any]] = {}
    places: Dict[str, Counter] = {}
    for booking in bookings:
        name = (booking.get("eventName") or "").strip()
        if not name:
            continue
        event = events.setdefault(name.lower(), {
            "name": name, "bookings": 0, "first_travel": None, "last_travel": None, "city": None,
            "country_code": None, "totals": {}, "by_type": {},
        })
        if str(booking.get("bookingStatus") or "").upper() in INACTIVE_STATUSES:
            continue
        amount = _amount(booking.get("grandTotal"))
        currency = str(booking.get("currency") or "").upper()
        if amount is None or len(currency) != 3:
            continue
        event["bookings"] += 1
        event["totals"][currency] = event["totals"].get(currency, Decimal(0)) + amount
        part = event["by_type"].setdefault((booking_type(booking.get("bookingType")), currency),
                                           {"amount": Decimal(0), "bookings": 0})
        part["amount"] += amount
        part["bookings"] += 1
        start = _date(booking.get("startDate"))
        if start and (event["first_travel"] is None or start < event["first_travel"]):
            event["first_travel"] = start
        end = _date(booking.get("endDate")) or start
        if end and (event["last_travel"] is None or end > event["last_travel"]):
            event["last_travel"] = end
        destination = booking.get("destination") if isinstance(booking.get("destination"), dict) else {}
        city = str(destination.get("city") or "").strip()
        if city:
            weight = 2 if str(booking.get("bookingType") or "").upper() == "HOTEL" else 1
            places.setdefault(name.lower(), Counter())[(city[:120], _country_code(destination.get("country")))] += weight
    for key, counter in places.items():
        events[key]["city"], events[key]["country_code"] = counter.most_common(1)[0][0]
    return events


def _clean(value: Any, limit: int) -> Optional[str]:
    text = str(value or "").strip()
    return text[:limit] or None


def traveller_key(person: Dict[str, Any]) -> Optional[str]:
    """Who a passenger is across bookings: Navan's person uuid, else their email (lower-cased), else their name.
    The email and name fall-backs are hashed so the key never holds personal details and fits 64 characters."""
    uuid = _clean(person.get("uuid") or person.get("id"), 64)
    if uuid:
        return uuid
    for kind, value in (("email", person.get("email")), ("name", _person_name(person))):
        text = str(value or "").strip().lower()
        if text:
            return f"{kind}:{hashlib.sha1(text.encode()).hexdigest()}"
    return None


def _person_name(person: Dict[str, Any]) -> Optional[str]:
    name = _clean(person.get("name"), 255)
    if name:
        return name
    parts = [str(person.get(k) or "").strip() for k in ("firstName", "lastName")]
    return _clean(" ".join(p for p in parts if p), 255)


def summarize_travellers(bookings: List[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """The people travelling on each Navan event's active bookings, keyed by the lower-cased event name, then by
    traveller_key.

    {"daas grand opening": {"<person uuid>": {"name": "...", "email": "...", "department": "...",
                                              "booking_types": {"FLIGHT", "HOTEL"}, "bookings": 2,
                                              "amounts": {"USD": Decimal("1234.50")},
                                              "first_travel": date, "last_travel": date}}}

    Only the name, email and department of each passenger are kept: never their passport, manager, cost centre,
    employee id or any other HR detail. A booking shared by several passengers counts once for each of them and
    its total is split evenly between them. Cancelled or voided bookings and passengers don't count; a booking
    with no passengers can't be put against anyone.
    """
    events: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for booking in bookings:
        name = (booking.get("eventName") or "").strip()
        if not name or str(booking.get("bookingStatus") or "").upper() in INACTIVE_STATUSES:
            continue
        passengers = []
        for passenger in booking.get("passengers") or []:
            if not isinstance(passenger, dict):
                continue
            if str(passenger.get("status") or "").upper() in INACTIVE_STATUSES:
                continue
            person = passenger.get("person") if isinstance(passenger.get("person"), dict) else {}
            key = traveller_key(person)
            if key:
                passengers.append((key, person))
        if not passengers:
            continue
        amount = _amount(booking.get("grandTotal"))
        currency = str(booking.get("currency") or "").upper()
        share = amount / len(passengers) if amount is not None and len(currency) == 3 else None
        start = _date(booking.get("startDate"))
        end = _date(booking.get("endDate")) or start
        kind = booking_type(booking.get("bookingType"))
        people = events.setdefault(name.lower(), {})
        for key, person in passengers:
            traveller = people.setdefault(key, {
                "name": None, "email": None, "department": None, "booking_types": set(), "bookings": 0,
                "amounts": {}, "first_travel": None, "last_travel": None,
            })
            traveller["name"] = traveller["name"] or _person_name(person)
            traveller["email"] = traveller["email"] or _clean(person.get("email"), 320)
            traveller["department"] = traveller["department"] or _clean(person.get("department"), 255)
            traveller["booking_types"].add(kind)
            traveller["bookings"] += 1
            if share is not None:
                traveller["amounts"][currency] = traveller["amounts"].get(currency, Decimal(0)) + share
            if start and (traveller["first_travel"] is None or start < traveller["first_travel"]):
                traveller["first_travel"] = start
            if end and (traveller["last_travel"] is None or end > traveller["last_travel"]):
                traveller["last_travel"] = end
    for people in events.values():
        for traveller in people.values():
            traveller["name"] = traveller["name"] or traveller["email"] or "Unknown traveller"
    return events
