from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

DemoSource = Literal["MANUAL", "MOBILE", "HUBSPOT"]


class DemoItem(BaseModel):
    id: UUID
    initiative_id: UUID
    demo_at: datetime
    demo_type_id: int
    demo_type_code: str
    demo_type_name: str
    # The organisation only; people's details stay in HubSpot
    company: str | None = None
    hubspot_url: str | None = None
    brand_id: int | None = None
    brand_name: str | None = None
    product_note: str | None = None
    given_by: UUID | None = None
    given_by_name: str | None = None
    outcome_id: int
    outcome_code: str
    outcome_name: str
    outcome_rank: int
    outcome_updated_at: datetime | None = None
    note: str | None = None
    source: DemoSource
    updated_at: datetime


class _DemoFields(BaseModel):
    """Shared clean-up for the text fields people type."""

    company: str | None = Field(default=None, max_length=255)
    hubspot_url: str | None = Field(default=None, max_length=500)
    product_note: str | None = Field(default=None, max_length=255)
    note: str | None = None

    @field_validator("company", "hubspot_url", "product_note", "note", mode="before", check_fields=False)
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("hubspot_url", check_fields=False)
    @classmethod
    def https_link(cls, value: str | None) -> str | None:
        if value and not value.lower().startswith("https://"):
            raise ValueError("HubSpot link must start with https://")
        return value


class DemoCreate(_DemoFields):
    demo_type_id: int
    # Defaults to now
    demo_at: datetime | None = None
    # Defaults to the event's brand
    brand_id: int | None = None
    # Defaults to the person logging it
    given_by: UUID | None = None
    # Defaults to "No outcome yet"
    outcome_id: int | None = None
    # MOBILE when logged from the phone app; HUBSPOT is reserved for a later sync
    source: Literal["MANUAL", "MOBILE"] = "MANUAL"


class DemoUpdate(_DemoFields):
    """Partial update: only the fields sent are changed. Send null to clear an optional field."""

    demo_type_id: int | None = None
    demo_at: datetime | None = None
    brand_id: int | None = None
    given_by: UUID | None = None
    outcome_id: int | None = None


class DemoCount(BaseModel):
    """One slice of the demos, e.g. one type, product or staff member."""

    key: str
    name: str
    count: int


class DemoFunnel(BaseModel):
    demos: int
    # Outcome known (anything but "No outcome yet")
    with_outcome: int
    # Follow-up booked or further along
    follow_ups: int
    # Deal opened or became a customer
    deals: int
    customers: int


class DemoSummary(BaseModel):
    total: int
    funnel: DemoFunnel
    # Follow-ups or further ÷ demos with a known outcome (null until one is known)
    follow_up_rate: float | None = None
    # Customers ÷ demos
    customer_rate: float | None = None
    by_type: list[DemoCount]
    by_outcome: list[DemoCount]
    by_brand: list[DemoCount]
    by_given_by: list[DemoCount]
    # The event's expected total cost (estimates included), in its own currency
    expected_cost: float
    currency: str
    cost_per_demo: float | None = None
    # Cost ÷ customers won through demos
    cost_per_customer: float | None = None
    # True when some costs are in another currency and aren't included
    mixed_currencies: bool
