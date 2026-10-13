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
