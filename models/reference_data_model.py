from typing import Literal

from pydantic import BaseModel


class ReferenceItem(BaseModel):
    id: int
    code: str
    name: str
    sort_order: int


class DescribedReferenceItem(ReferenceItem):
    description: str | None = None


class ChannelItem(ReferenceItem):
    sub_channel_ids: list[int]


class GoalItem(ReferenceItem):
    funnel_stage_id: int


class ExpenseTypeItem(DescribedReferenceItem):
    # Where costs of this type come from
    source: Literal["PORTAL", "TRAVEL_SYSTEM"]


class SpendCategoryItem(ReferenceItem):
    expense_type_id: int
    applies_to: Literal["ALL", "EVENTS", "ADS"]
    # False when costs only arrive from another system (e.g. travel)
    manual_entry_allowed: bool
    default_cost_type: Literal["OPEX", "CAPEX"] | None = None
    help_text: str | None = None


class OutcomeMetricItem(ReferenceItem):
    # COUNT for things counted; MONEY for pipeline and revenue
    unit: Literal["COUNT", "MONEY"]


class EvaluationDefaultItem(BaseModel):
    """What success means by default for a sub-channel (pre-fills the goal, KPI and maturity window)."""

    sub_channel_id: int
    funnel_stage_id: int | None = None
    goal_id: int | None = None
    primary_kpi_id: int | None = None
    maturity_days: int


class DemoOutcomeItem(ReferenceItem):
    # How far along the funnel: 0 no outcome yet / not a fit, 1 follow-up … 4 became a customer
    funnel_rank: int


class ReferenceDataResponse(BaseModel):
    brands: list[ReferenceItem]
    channels: list[ChannelItem]
    sub_channels: list[ReferenceItem]
    funnel_stages: list[ReferenceItem]
    goals: list[GoalItem]
    primary_kpis: list[ReferenceItem]
    performance_ratings: list[DescribedReferenceItem]
    scale_or_cut_options: list[DescribedReferenceItem]
    regions: list[ReferenceItem]
    event_types: list[ReferenceItem]
    # Budget markets (London, North America, ...); Unassigned last
    markets: list[ReferenceItem]
    expense_types: list[ExpenseTypeItem]
    spend_categories: list[SpendCategoryItem]
    outcome_metrics: list[OutcomeMetricItem]
    # Channel code → primary KPI codes it can use; channels not listed can use any
    kpi_choices_by_channel: dict[str, list[str]]
    # Channel code → result codes it can record, in form order; channels not listed can record any
    result_metrics_by_channel: dict[str, list[str]]
    evaluation_defaults: list[EvaluationDefaultItem]
    demo_types: list[ReferenceItem]
    demo_outcomes: list[DemoOutcomeItem]
