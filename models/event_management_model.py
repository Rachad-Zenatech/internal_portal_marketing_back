from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, field_validator

StaffRole = Literal["LEAD", "BOOTH_STAFF", "DEMO", "SPEAKER", "SETUP", "EXECUTIVE", "OTHER"]
EventTiming = Literal["NOW", "UPCOMING", "PAST", "UNSCHEDULED"]


class StaffAssignment(BaseModel):
    id: UUID
    initiative_id: UUID
    user_id: UUID
    full_name: str | None = None
    email: str
    role: StaffRole
    notes: str | None = None
    updated_at: datetime


class _StaffFields(BaseModel):
    notes: str | None = None

    @field_validator("notes", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class StaffAssignmentCreate(_StaffFields):
    user_id: UUID
    role: StaffRole = "BOOTH_STAFF"


class StaffAssignmentUpdate(_StaffFields):
    """Partial update: only the fields sent are changed. Send null to clear the notes."""

    role: StaffRole | None = None


class EventOverviewItem(BaseModel):
    """One event card on the event management page: what it is, and what still needs doing."""

    id: UUID
    name: str
    event_code: str | None = None
    brand_name: str
    sub_channel_code: str | None = None
    sub_channel_name: str | None = None
    status: str
    start_date: date | None = None
    end_date: date | None = None
    timing: EventTiming
    # Days until it starts (UPCOMING) or since it ended (PAST)
    days_away: int | None = None
    venue: str | None = None
    city: str | None = None
    country_code: str | None = None
    platform: str | None = None
    owner_name: str | None = None
    budget_amount: float | None = None
    currency: str
    expected_cost: float
    actual_cost: float
    cost_lines: int
    # The planned travel spend (flights, hotels, ground transport and stipends; the estimate Navan costs are compared
    # with), and the travel costs against it (Navan and travel entered here) in the event's currency: all of them
    # (expected) and invoiced + paid (spent)
    travel_budget_amount: float | None = None
    travel_expected: float = 0
    travel_spent: float = 0
    # Navan's travel estimate for the event (typed in, or from the Navan sync), and the total of its active Navan
    # bookings (the Navan cost lines), both in the event's currency; null when not known
    navan_estimate_amount: float | None = None
    navan_actual: float | None = None
    staff_count: int
    demos_given: int
    demos_booked: int
    leads: float | None = None
    has_results: bool
