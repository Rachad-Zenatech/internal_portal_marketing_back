from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# --- Results (outcomes) ---


class OutcomeValue(BaseModel):
    period_start: date
    metric_code: str
    value: float
    currency: str | None = None
    # MANUAL when typed in; DEMO_LOG when counted from the event's demo log; other tools' names when synced
    source: str
    recorded_at: datetime | None = None
    recorded_by_name: str | None = None


class OutcomeList(BaseModel):
    # Months that have any result, oldest first
    periods: list[date]
    values: list[OutcomeValue]
    # True when Demos are counted from the demo log (so they can't be typed in)
    demos_from_log: bool


class OutcomeInput(BaseModel):
    metric_code: str = Field(min_length=1, max_length=50)
    # null removes the value typed in for this month
    value: float | None = Field(default=None, ge=0, le=99_999_999_999_999)
    # Pipeline and revenue only; defaults to the initiative's currency
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")

    @field_validator("metric_code")
    @classmethod
    def upper_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class OutcomePeriodInput(BaseModel):
    values: list[OutcomeInput] = Field(max_length=50)


# --- Evaluation settings ---


class KpiRef(BaseModel):
    id: int
    code: str
    name: str
    direction: Literal["lower", "higher"] | None = None
    unit: Literal["MONEY", "PERCENT", "COUNT"] | None = None
    # False for KPIs that can't be worked out yet (Conversion Rate, LTV/CAC)
    supported: bool


class Evaluation(BaseModel):
    # The initiative's own primary KPI, or the sub-channel's default when it has none
    primary_kpi: KpiRef | None = None
    kpi_from_default: bool
    target_value: float | None = None
    maturity_days: int
    revenue_basis: Literal["CLOSED_REVENUE"]
    # True until someone saves settings for this initiative
    is_default: bool
    updated_at: datetime | None = None
    updated_by_name: str | None = None


class EvaluationInput(BaseModel):
    # In the KPI's unit: money for cost KPIs, % for CTR and ROI, a count for Impressions
    target_value: float | None = Field(default=None, ge=0, le=999_999_999_999)
    maturity_days: int = Field(default=180, ge=0, le=730)


# --- Decisions ---


class Decision(BaseModel):
    id: UUID
    scale_or_cut_id: int
    code: str
    name: str
    description: str | None = None
    suggested_rating: str | None = None
    note: str | None = None
    decided_by_name: str | None = None
    decided_at: datetime


class DecisionInput(BaseModel):
    scale_or_cut_id: int
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("note", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


# --- Performance (the calculated view) ---


class LadderStep(BaseModel):
    code: str
    name: str
    count: float
    # Cost ÷ count, in the initiative's currency
    cost_per: float | None = None
    # % of the step before that made it to this one
    from_previous: float | None = None
    previous_name: str | None = None


class ExcludedAmount(BaseModel):
    currency: str
    amount: float


class Rating(BaseModel):
    code: Literal["HIGH", "MEDIUM", "LOW", "TEST"]
    name: str
    reason: str
    # How far the KPI is from its target, in %
    gap_percent: float | None = None


class Performance(BaseModel):
    currency: str
    # The expected total cost (estimates included), in the initiative's currency
    cost: float
    costs_excluded: list[ExcludedAmount]
    # Every result, summed across months (demos from the demo log when there is one)
    totals: dict[str, float]
    revenue: float | None = None
    pipeline: float | None = None
    # Revenue or pipeline recorded in another currency, not included
    revenue_excluded: list[ExcludedAmount]
    ladder: list[LadderStep]
    ctr: float | None = None
    cpc: float | None = None
    cpm: float | None = None
    roi_percent: float | None = None
    # PROFIT, BROKE_EVEN, LOST_MONEY or NO_REVENUE (null without costs)
    money_outcome: Literal["PROFIT", "BROKE_EVEN", "LOST_MONEY", "NO_REVENUE"] | None = None
    has_results: bool
    demos_from_log: bool
    evaluation: Evaluation
    kpi_actual: float | None = None
    matures_on: date | None = None
    rating: Rating
    suggested_decision: str
    latest_decision: Decision | None = None
