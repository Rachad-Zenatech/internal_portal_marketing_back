"""Navan travel cost sync: run summaries and status."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class NavanTotal(BaseModel):
    currency: str
    amount: float


class NavanEvent(BaseModel):
    """A Navan event seen in the bookings, with its active booking total and the event it's linked to (if any)."""

    name: str
    bookings: int
    totals: list[NavanTotal]
    first_travel: str | None = None
    initiative_id: str | None = None
    initiative_name: str | None = None
    # SET: the event's "Navan event" names it. NAME_MATCH: the event has the same name.
    link: Literal["SET", "NAME_MATCH"] | None = None


class NavanSyncSummary(BaseModel):
    bookings: int
    linked: int
    # Events added here because Navan had bookings for them
    events_created: int = 0
    # Cost lines
    created: int
    updated: int
    removed: int
    conflicts: int
    events: list[NavanEvent]


class NavanSyncStatus(BaseModel):
    enabled: bool
    configured: bool
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    last_run_summary: NavanSyncSummary | None = None
