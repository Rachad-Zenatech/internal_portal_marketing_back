"""Generated Navan bookings for the demo events, used by the Navan sync when NAVAN_MODE=demo.

Only demo events get bookings: events in the Events channel whose name starts with DEMO_EVENT_PREFIX (added by
scripts/demo_events.py), not drafts and not cancelled. So demo mode never touches real events or their Navan costs.

Each demo event's bookings are generated from a random generator seeded by the event's id and dates, so every run
gives the same bookings until the event's dates change:
- 3 to 12 travellers (as many as the event's travel budget pays for, give or take one, when it has one in USD),
  each with a return flight, hotel nights over the event's dates (from the night before it
  starts) and, for about a third of them, a hire car. Prices are in USD. Travellers are made up ("Demo Traveller 3",
  with an example.com email) and are the passenger on each of their bookings, as in Navan.
- A few bookings are cancelled or voided (a flight changed and rebooked, a hotel booked twice, a trip called off).
- Each traveller books on a day 7 to 75 days before the event; only bookings made by today exist, so an event that
  has already started is fully booked and an upcoming one fills up as its date gets closer.
- The estimate is what's booked plus a forecast for the travellers who haven't booked yet.
"""

import datetime
import random
from decimal import Decimal
from typing import Any, Dict, List, Optional

from postgresql_db.database import get_pool

# The name every demo event starts with (see scripts/demo_events.py)
DEMO_EVENT_PREFIX = "[Demo] "
CURRENCY = "USD"

# Countries that fly domestically (cheaper flights)
_DOMESTIC = {"US", "CA"}
# Demo travellers' departments, taken in turn
_DEPARTMENTS = ("Sales", "Marketing", "Customer Success", "Product", "Engineering")

_DEMO_EVENTS_SQL = """
    SELECT i.id, i.name, i.start_date, i.end_date, i.status, i.currency, ed.navan_event_name, ed.city,
           ed.country_code, ed.travel_budget_amount
    FROM initiatives i
    JOIN channels c ON c.id = i.channel_id AND c.code = 'EVENTS'
    JOIN event_details ed ON ed.initiative_id = i.id
    WHERE left(i.name, length($1)) = $1
      AND i.status NOT IN ('DRAFT', 'CANCELLED')
      AND i.start_date IS NOT NULL
    ORDER BY i.start_date, i.id
"""


def demo_events_sql(alias: str = "i") -> str:
    """A SQL condition that is true when the initiative aliased `alias` is a demo event (by its name)."""
    return f"left({alias}.name, {len(DEMO_EVENT_PREFIX)}) = '{DEMO_EVENT_PREFIX}'"


def _money(value: float) -> Decimal:
    return Decimal(str(round(value, 2)))


def demo_traveller(event_id, traveller: int) -> Dict[str, Any]:
    """The made-up person on a demo traveller's bookings, like Navan's passenger.person (example.com emails)."""
    number = traveller + 1
    return {
        "uuid": f"demo-{str(event_id)[:8]}-{traveller:02d}",
        "name": f"Demo Traveller {number}",
        "email": f"demo.traveller{number}@example.com",
        "department": _DEPARTMENTS[traveller % len(_DEPARTMENTS)],
    }


def _booking(event_id, traveller: int, kind: str, n: int, *, name: str, total: Decimal, status: str,
             start: datetime.date, end: datetime.date, booked_on: datetime.date, city: Optional[str],
             country: Optional[str]) -> Dict[str, Any]:
    return {
        "bookingId": f"demo-{str(event_id)[:8]}-{traveller:02d}-{kind.lower()}-{n}",
        "eventName": name,
        "bookingType": kind,
        "bookingStatus": status,
        "grandTotal": str(total),
        "currency": CURRENCY,
        "startDate": f"{start.isoformat()}T09:00:00",
        "endDate": f"{end.isoformat()}T17:00:00",
        "createdDate": f"{booked_on.isoformat()}T12:00:00",
        "destination": {"city": city, "country": country},
        "passengers": [{"person": demo_traveller(event_id, traveller), "status": status, "travelerType": "EMPLOYEE"}],
    }


def _travellers(rng: random.Random, event, domestic: bool, nights: int) -> int:
    """3 to 12; about as many as the travel budget pays for when the event has one in USD."""
    budget = event.get("travel_budget_amount")
    if budget is None or event.get("currency") != CURRENCY:
        return rng.randint(3, 12)
    per_traveller = (500 if domestic else 1400) + 250 * nights + 0.33 * 75 * (nights + 1)
    return max(3, min(12, round(float(budget) / per_traveller) + rng.randint(-1, 1)))


def generate_event_bookings(event, today: datetime.date) -> tuple[List[Dict[str, Any]], Decimal]:
    """(bookings, estimate) for one demo event, the same on every run for the same event, dates, travel budget and
    day."""
    start: datetime.date = event["start_date"]
    end: datetime.date = event["end_date"] or start
    name = event["navan_event_name"] or event["name"]
    rng = random.Random(f"{event['id']}:{start.isoformat()}:{end.isoformat()}")
    domestic = (event["country_code"] or "US") in _DOMESTIC
    nights = (end - start).days + 1  # from the night before it starts to the last day
    arrive, leave = start - datetime.timedelta(days=1), end

    bookings: List[Dict[str, Any]] = []
    booked = Decimal(0)
    forecast = Decimal(0)
    for traveller in range(_travellers(rng, event, domestic, nights)):
        # Every value is drawn whether or not the traveller has booked yet, so the data doesn't shift as days pass
        booked_on = start - datetime.timedelta(days=rng.randint(7, 75))
        flight = _money(rng.uniform(280, 720) if domestic else rng.uniform(950, 1850))
        hotel = _money(rng.uniform(170, 330) * nights)
        car = _money(rng.uniform(55, 95) * (nights + 1)) if rng.random() < 0.33 else None
        flight_changed = rng.random() < 0.12  # first flight cancelled, then rebooked
        first_flight = _money(float(flight) * rng.uniform(0.85, 1.15))
        hotel_twice = rng.random() < 0.06  # a duplicate hotel booking, voided
        called_off = rng.random() < 0.05  # the trip was cancelled altogether
        if called_off:
            if booked_on <= today:
                bookings.append(_booking(event["id"], traveller, "FLIGHT", 1, name=name, total=flight, status="CANCELED",
                                         start=arrive, end=leave, booked_on=booked_on, city=event["city"],
                                         country=event["country_code"]))
            continue
        trip = flight + hotel + (car or 0)
        if booked_on > today:
            forecast += trip
            continue

        def add(kind: str, n: int, total: Decimal, status: str = "TICKETED") -> None:
            bookings.append(_booking(event["id"], traveller, kind, n, name=name, total=total, status=status,
                                     start=arrive, end=leave, booked_on=booked_on, city=event["city"],
                                     country=event["country_code"]))

        if flight_changed:
            add("FLIGHT", 1, first_flight, "CANCELED")
        add("FLIGHT", 2, flight)
        add("HOTEL", 1, hotel, "CONFIRMED")
        if hotel_twice:
            add("HOTEL", 2, hotel, "VOIDED")
        if car is not None:
            add("CAR", 1, car, "CONFIRMED")
        booked += trip
    return bookings, (booked + forecast).quantize(Decimal("0.01"))


async def fetch_demo_navan_data(today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Generated bookings and estimates for every demo event, in the shape the sync reads from Navan.

    {"bookings": [...], "estimates": {"[demo] saas north expo 2026": {"name": "[Demo] SaaS North Expo 2026", "currency": "USD",
                                                    "amount": Decimal}}}
    """
    today = today or datetime.date.today()
    pool = get_pool()
    async with pool.acquire() as conn:
        events = await conn.fetch(_DEMO_EVENTS_SQL, DEMO_EVENT_PREFIX)
    bookings: List[Dict[str, Any]] = []
    estimates: Dict[str, Dict[str, Any]] = {}
    for event in events:
        event_bookings, estimate = generate_event_bookings(event, today)
        bookings += event_bookings
        name = (event["navan_event_name"] or event["name"]).strip()
        estimates[name.lower()] = {"name": name, "currency": CURRENCY, "amount": estimate}
    return {"bookings": bookings, "estimates": estimates}
