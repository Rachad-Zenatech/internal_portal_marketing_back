"""Events Overview: budgets, actual spend, prior year spend, results and ROI of events (GET /api/events/performance)."""
from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Period(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # Either end may be null for no limit
    date_from: date | None = Field(default=None, alias="from")
    date_to: date | None = Field(default=None, alias="to")


class PreviousFigures(BaseModel):
    """The same figures for the previous period; null where they can't be worked out (no events, no budget...)."""

    approved_budget: float | None = None
    actual_spend: float | None = None
    budget_variance: float | None = None
    prior_year_spend: float | None = None
    leads: float | None = None
    cost_per_lead: float | None = None


class EventPerformanceTotals(BaseModel):
    """One row per currency (money is never added across currencies)."""

    currency: str
    event_count: int
    # Budgets of the events that have one; null when none has
    approved_budget: float | None = None
    # Invoiced and paid cost lines in the event's currency
    actual_spend: float | None = None
    # Actual spend of the events with a budget minus approved_budget; null without a budget
    budget_variance: float | None = None
    # Actual spend of events (same filters) in the same period a year earlier; null when there were none
    prior_year_spend: float | None = None
    leads: float | None = None
    # actual_spend / leads; null when leads are missing or 0
    cost_per_lead: float | None = None
    previous: PreviousFigures


class MonthlyRow(BaseModel):
    """Budget vs actual vs prior year, by the month the events start (0 when nothing that month)."""

    month: date
    currency: str
    approved_budget: float
    actual_spend: float
    prior_year_spend: float


class Funnel(BaseModel):
    """All events in scope, whatever their currency; null where nothing was recorded."""

    attendees: float | None = None
    leads: float | None = None
    qualified_leads: float | None = None
    demos: float | None = None
    customers: float | None = None


class CostCategories(BaseModel):
    """Actual cost split into the seven categories of the cost breakdown."""

    BOOTH_SPACE: float
    TRAVEL: float
    GROUND_TRANSPORT: float
    STAFF_TIME: float
    MEALS_PER_DIEMS: float
    MATERIALS: float
    OTHER: float


class CurrencyCostBreakdown(BaseModel):
    """Actual spend of one currency's events split into the seven categories."""

    currency: str
    BOOTH_SPACE: float
    TRAVEL: float
    GROUND_TRANSPORT: float
    STAFF_TIME: float
    MEALS_PER_DIEMS: float
    MATERIALS: float
    OTHER: float


class EventPerformance(BaseModel):
    id: UUID
    name: str
    event_code: str | None = None
    brand_name: str
    market_name: str | None = None
    city: str | None = None
    country_code: str | None = None
    event_type_name: str | None = None
    owner_name: str | None = None
    start_date: date
    end_date: date | None = None
    # The initiative's status
    status: str
    currency: str
    approved_budget: float | None = None
    # Invoiced and paid, in the event's currency
    actual_cost: float
    # Estimates and commitments not invoiced yet
    awaiting_cost: float
    # Actual cost of last year's event with the same name, when it's in the same currency; null if none
    prior_year_cost: float | None = None
    prior_year_event_id: UUID | None = None
    # actual_cost - approved_budget; null without a budget
    variance: float | None = None
    # Null when not recorded
    attendees: float | None = None
    leads: float | None = None
    qualified_leads: float | None = None
    demos: float | None = None
    customers: float | None = None
    revenue: float | None = None
    cost_per_lead: float | None = None
    # (revenue - actual_cost) / actual_cost * 100; null without revenue or cost
    roi_percent: float | None = None
    cost_categories: CostCategories
    # Cost lines in another currency than the event's: left out, never converted
    other_currency_lines: int


class EventPerformanceResponse(BaseModel):
    period: Period
    # Same length, immediately before the period; null unless the period has both ends
    previous_period: Period | None = None
    totals: list[EventPerformanceTotals]
    monthly: list[MonthlyRow]
    funnel: Funnel
    cost_breakdown: list[CurrencyCostBreakdown]
    events: list[EventPerformance]
