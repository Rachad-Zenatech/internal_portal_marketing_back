from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

StaffRole = Literal["LEAD", "BOOTH_STAFF", "DEMO", "SPEAKER", "SETUP", "EXECUTIVE", "OTHER"]
TravelStatus = Literal["REQUESTED", "BOOKED", "CANCELLED"]
# Worked out from the details: what the travel system can act on
TravelReadiness = Literal["NOT_TRAVELLING", "MISSING_DETAILS", "READY", "REQUESTED", "BOOKED", "CANCELLED"]
EventTiming = Literal["NOW", "UPCOMING", "PAST", "UNSCHEDULED"]


class StaffAssignment(BaseModel):
    id: UUID
    initiative_id: UUID
    user_id: UUID
    full_name: str | None = None
    email: str
    role: StaffRole
    travel_needed: bool
    departs_from: str | None = None
    arrive_on: date | None = None
    depart_on: date | None = None
    needs_flight: bool
    needs_hotel: bool
    needs_ground_transport: bool
    needs_stipend: bool
    cargo_note: str | None = None
    notes: str | None = None
    travel_status: TravelStatus | None = None
    travel_status_at: datetime | None = None
    readiness: TravelReadiness
    # What's still needed before the travel system can book them, e.g. ["arrival date", "departure city"]
    missing: list[str]
    updated_at: datetime


class _StaffFields(BaseModel):
    departs_from: str | None = Field(default=None, max_length=120)
    cargo_note: str | None = Field(default=None, max_length=255)
    notes: str | None = None

    @field_validator("departs_from", "cargo_note", "notes", mode="before", check_fields=False)
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def check_dates(self):
        arrive, depart = getattr(self, "arrive_on", None), getattr(self, "depart_on", None)
        if arrive and depart and depart < arrive:
            raise ValueError("The departure date can't be before the arrival date.")
        return self


class StaffAssignmentCreate(_StaffFields):
    user_id: UUID
    role: StaffRole = "BOOTH_STAFF"
    travel_needed: bool = True
    # Default to the event's dates when travel is needed
    arrive_on: date | None = None
    depart_on: date | None = None
    needs_flight: bool = True
    needs_hotel: bool = True
    needs_ground_transport: bool = True
    needs_stipend: bool = True


class StaffAssignmentUpdate(_StaffFields):
    """Partial update: only the fields sent are changed. Send null to clear an optional field."""

    role: StaffRole | None = None
    travel_needed: bool | None = None
    arrive_on: date | None = None
    depart_on: date | None = None
    needs_flight: bool | None = None
    needs_hotel: bool | None = None
    needs_ground_transport: bool | None = None
    needs_stipend: bool | None = None


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
    # The part of the budget for flights, hotels, ground transport and stipends, and the travel system's costs
    # against it in the event's currency: all of them (expected) and invoiced + paid (spent)
    travel_budget_amount: float | None = None
    travel_expected: float = 0
    travel_spent: float = 0
    staff_count: int
    staff_ready: int
    staff_missing_details: int
    staff_booked: int
    demos_given: int
    demos_booked: int
    leads: float | None = None
    has_results: bool


class TravelFeedStaff(BaseModel):
    assignment_id: UUID
    user_id: UUID
    full_name: str | None = None
    email: str
    role: StaffRole
    departs_from: str | None = None
    arrive_on: date | None = None
    depart_on: date | None = None
    needs_flight: bool
    needs_hotel: bool
    needs_ground_transport: bool
    needs_stipend: bool
    cargo_note: str | None = None
    notes: str | None = None
    # READY when the travel system has what it needs; MISSING_DETAILS otherwise (listed in `missing`)
    readiness: TravelReadiness
    missing: list[str]
    updated_at: datetime


class TravelFeedEvent(BaseModel):
    event_code: str
    initiative_id: UUID
    name: str
    status: str
    brand: str
    sub_channel: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    venue: str | None = None
    city: str | None = None
    country_code: str | None = None
    platform: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    # The event's travel budget (always less than the event budget) and the currency it's in
    travel_budget_amount: float | None = None
    currency: str
    # Only staff who need travel
    staff: list[TravelFeedStaff]
    updated_at: datetime


class TravelStatusReport(BaseModel):
    """The travel system reporting how far booking has got for one assignment."""

    assignment_id: UUID
    travel_status: TravelStatus
