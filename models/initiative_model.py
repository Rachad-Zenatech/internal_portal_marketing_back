import re
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.channel_details_model import DataSource, LinkSystem

# DRAFT: saved part-way to finish later; the event rules (e.g. city and country) apply once it leaves Draft
InitiativeStatus = Literal["DRAFT", "PLANNED", "ACTIVE", "COMPLETED", "PAUSED", "CANCELLED"]

# Why an initiative needs attention:
# OVER_BUDGET actual spend is over the budget; NO_BUDGET no budget (not a draft);
# NO_TRACKING active or completed, started 14+ days ago, with no costs, no results and no campaign links;
# NO_RESULTS ended (or completed) with no results recorded (not a draft or cancelled); NO_DATES no start date
# (not a draft)
AttentionReason = Literal["OVER_BUDGET", "NO_BUDGET", "NO_TRACKING", "NO_RESULTS", "NO_DATES"]

KPI_TARGET_NEEDS_KPI = "Choose a primary KPI before setting a KPI target."
LINKED_NEEDS_LINK = "Add at least one campaign link, or choose another data source."
DUPLICATE_LINK = "The same campaign is listed twice."
_MAX_AMOUNT = 999_999_999_999


class PrimaryKpi(BaseModel):
    id: int
    code: str
    name: str
    unit: Literal["COUNT", "MONEY", "PERCENT"]


class InitiativeLink(BaseModel):
    id: UUID
    system: LinkSystem
    external_id: str
    account_id: str | None = None
    created_at: datetime


class InitiativeLinkInput(BaseModel):
    """An external campaign: the system it lives in, its campaign ID there and, optionally, the account ID."""

    model_config = ConfigDict(extra="forbid")

    system: LinkSystem
    external_id: str = Field(max_length=100)
    account_id: str | None = Field(default=None, max_length=100)

    @field_validator("external_id", "account_id", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("external_id")
    @classmethod
    def require_external_id(cls, value: str) -> str:
        if not value:
            raise ValueError("Campaign ID is required")
        return value

    @field_validator("account_id")
    @classmethod
    def blank_account(cls, value: str | None) -> str | None:
        return value or None


class EvaluationInput(BaseModel):
    """Evaluation settings set from the wizard. Revenue basis is always closed revenue, so it isn't sent."""

    model_config = ConfigDict(extra="forbid")

    # Days after the end before the verdict is final; empty keeps the current window (the sub-channel's default)
    maturity_days: int | None = Field(default=None, ge=0, le=730)


class InitiativeListItem(BaseModel):
    id: UUID
    name: str
    brand_id: int
    brand_name: str
    channel_id: int
    channel_name: str
    sub_channel_id: int | None = None
    sub_channel_name: str | None = None
    funnel_stage_id: int | None = None
    funnel_stage_name: str | None = None
    goal_id: int | None = None
    goal_name: str | None = None
    owner_user_id: UUID | None = None
    owner_name: str | None = None
    # The market whose monthly budget its costs count against; empty counts as Unassigned
    market_id: int | None = None
    market_name: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    budget_amount: float | None = None
    currency: str
    status: InitiativeStatus
    # Event details — null for initiatives outside the Events channel
    event_code: str | None = None
    event_type_id: int | None = None
    event_type_name: str | None = None
    venue: str | None = None
    city: str | None = None
    country_code: str | None = None
    region_id: int | None = None
    region_name: str | None = None
    platform: str | None = None
    event_url: str | None = None
    booth_size_sqm: float | None = None
    # Planned travel spend (flights, hotels, ground transport and stipends) in the initiative's currency: the
    # estimate Navan costs are compared with
    travel_budget_amount: float | None = None
    # The Navan event its travel bookings come from; empty means a Navan event with the same name
    navan_event_name: str | None = None
    # Navan's travel estimate for the event in the initiative's currency: typed in, or set by the Navan sync when
    # Navan gives one (only its demo mode does today)
    navan_estimate_amount: float | None = None
    # Costs in the initiative's currency: everything expected (estimates included), and invoiced + paid
    expected_total: float = 0
    actual_amount: float = 0
    # Some costs are in another currency and aren't included in the totals above
    has_mixed_currencies: bool = False
    description: str | None = None
    # Optional; shown with its target, never used by the worth-it verdict
    primary_kpi: PrimaryKpi | None = None
    kpi_target: float | None = None
    data_source: DataSource = "MANUAL"
    # Channel-specific fields (see models/channel_details_model.py); empty values are left out
    channel_details: dict[str, Any] = Field(default_factory=dict)
    links: list[InitiativeLink] = Field(default_factory=list)
    # Invoiced + paid costs in the initiative's currency (the same figure as actual_amount)
    actual_spend: float = 0
    # actual_spend / budget * 100, one decimal; null without a budget above zero
    progress_percent: float | None = None
    needs_attention: bool = False
    attention_reasons: list[AttentionReason] = Field(default_factory=list)


class InitiativeListResponse(BaseModel):
    items: list[InitiativeListItem]
    total: int
    limit: int
    offset: int


class BudgetAmount(BaseModel):
    currency: str
    amount: float


class InitiativeCounts(BaseModel):
    total: int
    active: int
    needs_attention: int
    # Sum of the matching initiatives' budgets, one entry per currency (largest first)
    allocated_budget: list[BudgetAmount]


class InitiativeSummary(InitiativeCounts):
    # The same figures for the period of the same length just before from/to; null unless both dates are given
    previous: InitiativeCounts | None = None


class ActivityItem(BaseModel):
    at: datetime
    # The person; "Navan", "HubSpot", ... when a sync made the change; "System" when neither is known
    actor_name: str | None = None
    title: str
    detail: str | None = None


class EventDetailsInput(BaseModel):
    """Event-only fields. The event code is generated by the database, never sent by the client."""

    event_type_id: int | None = None
    venue: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    country_code: str | None = None
    region_id: int | None = None
    # Virtual events and webinars only
    platform: str | None = Field(default=None, max_length=80)
    event_url: str | None = Field(default=None, max_length=500)
    # In-person events only, always in square metres (the form converts square feet)
    booth_size_sqm: float | None = Field(default=None, gt=0, le=999_999.99)
    # Planned travel spend: the part of the event budget (same currency) for flights, hotels, ground transport and
    # stipends, and the estimate Navan costs are compared with. Has to be smaller than the event budget.
    travel_budget_amount: float | None = Field(default=None, ge=0, le=999_999_999_999)
    # The Navan event its travel bookings come from, exactly as named in Navan. Empty: a Navan event with the
    # same name as this event is used.
    navan_event_name: str | None = Field(default=None, max_length=255)
    # Navan's travel estimate for the event, in the event's currency. Unlike the travel budget it isn't limited by
    # the event budget. The Navan sync may update it when Navan gives an estimate.
    navan_estimate_amount: float | None = Field(default=None, ge=0, le=999_999_999_999)

    @field_validator("venue", "city", "country_code", "platform", "event_url", "navan_event_name", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("country_code")
    @classmethod
    def check_country_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.upper()
        if not re.fullmatch(r"[A-Z]{2}", value):
            raise ValueError("Country must be a two-letter code, e.g. US")
        return value

    @field_validator("event_url")
    @classmethod
    def check_event_url(cls, value: str | None) -> str | None:
        if value is not None and not value.lower().startswith(("http://", "https://")):
            raise ValueError("Event link must start with http:// or https://")
        return value


class EventDetailsUpdate(EventDetailsInput):
    """Partial: only the fields sent change; send null to clear one. The event code can't be changed."""

    # Reject unknown fields (e.g. event_code) instead of silently ignoring them
    model_config = ConfigDict(extra="forbid")


class InitiativeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    brand_id: int
    channel_id: int
    sub_channel_id: int | None = None
    funnel_stage_id: int | None = None
    goal_id: int | None = None
    market_id: int | None = None
    # Defaults to the user creating the initiative
    owner_user_id: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    budget_amount: float | None = Field(default=None, ge=0, le=999_999_999_999)
    currency: str = Field(default="USD", pattern=r"^[A-Za-z]{3}$")
    # Drafts only need a name, brand and channel; the other required fields are checked once it leaves Draft
    status: InitiativeStatus = "PLANNED"
    # Only for initiatives in the Events channel
    event: EventDetailsInput | None = None
    description: str | None = Field(default=None, max_length=1000)
    primary_kpi_id: int | None = None
    kpi_target: float | None = Field(default=None, ge=0, le=_MAX_AMOUNT)
    data_source: DataSource = "MANUAL"
    # Checked against the channel's own fields; unknown fields are refused
    channel_details: dict[str, Any] | None = None
    links: list[InitiativeLinkInput] = Field(default_factory=list, max_length=20)
    evaluation: EvaluationInput | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name is required")
        return value

    @field_validator("description", mode="before")
    @classmethod
    def blank_description(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def check_dates_and_goal(self) -> "InitiativeCreate":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("End date can't be before the start date")
        if self.goal_id is not None and self.funnel_stage_id is None:
            raise ValueError("Choose a funnel stage before choosing a goal")
        if self.kpi_target is not None and self.primary_kpi_id is None:
            raise ValueError(KPI_TARGET_NEEDS_KPI)
        if self.data_source == "LINKED" and not self.links:
            raise ValueError(LINKED_NEEDS_LINK)
        _check_unique_links(self.links)
        return self


def _check_unique_links(links: list[InitiativeLinkInput] | None) -> None:
    keys = [(link.system, link.external_id) for link in links or []]
    if len(keys) != len(set(keys)):
        raise ValueError(DUPLICATE_LINK)


class InitiativeUpdate(BaseModel):
    """Partial update: only the fields sent are changed. Send null to clear an optional field."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    brand_id: int | None = None
    channel_id: int | None = None
    sub_channel_id: int | None = None
    funnel_stage_id: int | None = None
    goal_id: int | None = None
    market_id: int | None = None
    owner_user_id: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    budget_amount: float | None = Field(default=None, ge=0, le=999_999_999_999)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    status: InitiativeStatus | None = None
    # Only for initiatives in the Events channel; only the event fields sent are changed
    event: EventDetailsUpdate | None = None
    description: str | None = Field(default=None, max_length=1000)
    # Clearing the KPI also clears its target
    primary_kpi_id: int | None = None
    kpi_target: float | None = Field(default=None, ge=0, le=_MAX_AMOUNT)
    data_source: DataSource | None = None
    # Replaces all of the channel details; null or {} clears them
    channel_details: dict[str, Any] | None = None
    # Replaces the whole set of links when sent; [] removes them all
    links: list[InitiativeLinkInput] | None = Field(default=None, max_length=20)
    evaluation: EvaluationInput | None = None

    @field_validator("description", mode="before")
    @classmethod
    def blank_description(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Name is required")
        return value

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @model_validator(mode="after")
    def check_required_fields(self) -> "InitiativeUpdate":
        # These can be changed but not cleared
        for field in ("name", "brand_id", "channel_id", "currency", "status", "data_source"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field.replace('_id', '').replace('_', ' ').capitalize()} can't be empty")
        if self.kpi_target is not None and "primary_kpi_id" in self.model_fields_set and self.primary_kpi_id is None:
            raise ValueError(KPI_TARGET_NEEDS_KPI)
        _check_unique_links(self.links)
        return self


class OwnerOption(BaseModel):
    id: UUID
    full_name: str | None = None
    email: str
