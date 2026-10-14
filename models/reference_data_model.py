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
    expense_types: list[ExpenseTypeItem]
    spend_categories: list[SpendCategoryItem]
