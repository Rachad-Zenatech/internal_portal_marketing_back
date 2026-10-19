"""Navan travel cost sync: run summaries and status."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class NavanTotal(BaseModel):
    currency: str
    amount: float


class NavanTypeTotal(BaseModel):
    """Active bookings of one type in one currency: one cost line on the linked event."""

    # FLIGHT, HOTEL, CAR, RAIL or OTHER
    booking_type: str
    currency: str
    amount: float
    bookings: int


class NavanEvent(BaseModel):
    """A Navan event seen in the bookings, with its active booking total and the event it's linked to (if any)."""

    name: str
    bookings: int
    # All active bookings per currency (the event's Navan actual)
    totals: list[NavanTotal]
    # The same split by booking type
    by_type: list[NavanTypeTotal] = []
    # Navan's estimate for the event, when the source gives one (only demo mode does)
    estimate: NavanTotal | None = None
    first_travel: str | None = None
    # People on its active bookings
    travellers: int = 0
    initiative_id: str | None = None
    initiative_name: str | None = None
    # SET: the event's "Navan event" names it. NAME_MATCH: the event has the same name.
    link: Literal["SET", "NAME_MATCH"] | None = None


class NavanSyncSummary(BaseModel):
    # Where the bookings came from: "live" (Navan) or "demo" (generated for the demo events)
    mode: Literal["live", "demo"] = "live"
    bookings: int
    linked: int
    # Events added here because Navan had bookings for them
    events_created: int = 0
    # Cost lines
    created: int
    updated: int
    removed: int
    conflicts: int
    # Events whose Navan estimate was set from the source's estimate
    estimates_updated: int = 0
    # Events whose status (by their dates) or missing city was brought up to date
    events_updated: int = 0
    # Travellers on linked events (event_travellers rows) added, changed and removed
    travellers_added: int = 0
    travellers_updated: int = 0
    travellers_removed: int = 0
    events: list[NavanEvent]


class NavanSyncStatus(BaseModel):
    # "demo": the sync uses generated data for the demo events, not Navan (NAVAN_MODE=demo); show "Demo data"
    mode: Literal["live", "demo"]
    enabled: bool
    configured: bool
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    last_run_summary: NavanSyncSummary | None = None
