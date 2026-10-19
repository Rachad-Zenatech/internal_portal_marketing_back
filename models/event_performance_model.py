"""Events Overview: how events perform, what they cost and what they brought in (GET /api/events/performance)."""
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

PerformanceTiming = Literal["UPCOMING", "NOW", "PAST"]
EventResult = Literal["UPCOMING", "TOO_EARLY", "SUCCESSFUL", "NOT_SUCCESSFUL", "AWAITING_REVENUE"]
MoneyOutcome = Literal["PROFIT", "BROKE_EVEN", "LOST_MONEY", "NO_REVENUE"]


class CostBreakdown(BaseModel):
    """Total event cost = Travel + Booth + Materials + Staff & other, in the event's currency."""

    TRAVEL: float
    BOOTH: float
    MATERIALS: float
    STAFF_OTHER: float


class _Numbers(BaseModel):
    cost: float
    cost_breakdown: CostBreakdown
    # Counts and money, or null when nothing was recorded
    visitors: float | None = None
    leads: float | None = None
    demos: float | None = None
    follow_ups: float | None = None
    customers: float | None = None
    revenue: float | None = None
    pipeline: float | None = None
    # Null when the divisor is missing or 0, or there's no cost
    cost_per_visitor: float | None = None
    cost_per_lead: float | None = None
    cost_per_demo: float | None = None
    cost_per_follow_up: float | None = None
    cost_per_customer: float | None = None
    roi_percent: float | None = None
    money_outcome: MoneyOutcome | None = None


class EventPerformance(_Numbers):
    id: UUID
    name: str
    event_code: str | None = None
    brand_name: str
    market_name: str | None = None
    sub_channel_code: str | None = None
    sub_channel_name: str | None = None
    city: str | None = None
    country_code: str | None = None
    platform: str | None = None
    start_date: date
    end_date: date | None = None
    timing: PerformanceTiming
    status: str
    currency: str
    budget_amount: float | None = None
    # Cost lines in another currency than the event's: left out of the cost, never converted
    other_currency_lines: int
    result: EventResult
    matures_on: date | None = None


class EventPerformanceTotals(_Numbers):
    """One row per currency, recomputed from the summed numbers (never an average of averages)."""

    currency: str
    event_count: int
    successful_count: int
    unsuccessful_count: int
    # Too early to judge, or waiting for revenue
    awaiting_count: int


class EventPerformanceResponse(BaseModel):
    events: list[EventPerformance]
    totals: list[EventPerformanceTotals]
