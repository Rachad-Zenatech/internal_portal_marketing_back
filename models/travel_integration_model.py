"""What this portal reads from the travel management system, and what the travel sync endpoints return.

Travel costs come from GET {TRAVEL_BACKEND_URL}/api/travel/integrations/marketing/costs (proposed contract).
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

TravelCostCategory = Literal["FLIGHT", "LODGING", "GROUND_TRANSPORT", "STIPEND", "CARGO", "MEALS", "OTHER"]
TravelCostStatus = Literal["APPROVED", "PAID", "CANCELLED", "REJECTED"]
CircuitStateName = Literal["CLOSED", "OPEN", "HALF_OPEN"]


class TravelCost(BaseModel):
    """One approved (or later paid, cancelled or rejected) travel cost for an event."""

    # The travel system's own id for the cost; stays the same across updates
    id: str = Field(min_length=1, max_length=100)
    event_code: str = Field(min_length=1, max_length=20)
    # Our event_staff id when the cost is for one person on the event
    assignment_id: str | None = None
    category: TravelCostCategory
    description: str | None = None
    vendor: str | None = None
    amount: Decimal = Field(max_digits=14, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    incurred_on: date
    status: TravelCostStatus
    paid_on: date | None = None
    updated_at: datetime

    @field_validator("currency", mode="before")
    @classmethod
    def _upper_currency(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("event_code", mode="before")
    @classmethod
    def _trim_code(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("description", "vendor", mode="before")
    @classmethod
    def _blank_to_none(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class TravelCostsResponse(BaseModel):
    """The response envelope. Items are validated one by one by the sync, so one bad cost can't block the rest."""

    costs: list[dict]
    server_time: datetime


class TravelSyncSkippedCost(BaseModel):
    # The travel system's id for the cost, when it sent one
    id: str | None = None
    reason: str


class TravelSyncSummary(BaseModel):
    fetched: int = 0
    created: int = 0
    updated: int = 0
    removed: int = 0
    skipped_unknown_event: int = 0
    # Changes the travel system sent for costs already reconciled here; those lines were left as they are
    conflicts: int = 0
    # Costs that didn't match the contract and were left out
    invalid: int = 0
    # Up to 20 of the costs left out (unknown event or invalid), with why; they are fetched again next run
    skipped: list[TravelSyncSkippedCost] = Field(default_factory=list)


class TravelSyncStatus(BaseModel):
    enabled: bool
    configured: bool
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    last_run_summary: TravelSyncSummary | None = None
    circuit_state: CircuitStateName
