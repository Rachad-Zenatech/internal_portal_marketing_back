"""The Paid Search board: entries typed in or uploaded from Marketing's PPC sheet, saved as the board shows them."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# The sheet's 24 columns, in board order (the frontend's PAID_SEARCH_FIELDS uses the same keys)
FIELD_KEYS = (
    "brand", "market", "period_start", "period_end", "channel", "sub_channel", "campaign_name", "primary_kpi",
    "budget_allocated", "actual_spend", "impressions", "clicks", "ctr", "cpc", "ads_conversions", "hs_contacts",
    "ads_conv_rate", "cpl", "mqls", "sqls", "last_update", "status", "report_link", "tracking_detail",
)

# Long enough for any cell people type; keeps one row from being unbounded
_MAX_TEXT = 2000
# The most entries one save may hold
MAX_ENTRIES = 5000

HubSpotLink = Literal["auto", "manual", "none"]


class PaidSearchEntry(BaseModel):
    """One board row. Every sheet value is text as typed, so a half-filled row can be saved."""

    id: UUID
    brand: str = Field(default="", max_length=_MAX_TEXT)
    market: str = Field(default="", max_length=_MAX_TEXT)
    period_start: str = Field(default="", max_length=_MAX_TEXT)
    period_end: str = Field(default="", max_length=_MAX_TEXT)
    channel: str = Field(default="", max_length=_MAX_TEXT)
    sub_channel: str = Field(default="", max_length=_MAX_TEXT)
    campaign_name: str = Field(default="", max_length=_MAX_TEXT)
    primary_kpi: str = Field(default="", max_length=_MAX_TEXT)
    budget_allocated: str = Field(default="", max_length=_MAX_TEXT)
    actual_spend: str = Field(default="", max_length=_MAX_TEXT)
    impressions: str = Field(default="", max_length=_MAX_TEXT)
    clicks: str = Field(default="", max_length=_MAX_TEXT)
    ctr: str = Field(default="", max_length=_MAX_TEXT)
    cpc: str = Field(default="", max_length=_MAX_TEXT)
    ads_conversions: str = Field(default="", max_length=_MAX_TEXT)
    hs_contacts: str = Field(default="", max_length=_MAX_TEXT)
    ads_conv_rate: str = Field(default="", max_length=_MAX_TEXT)
    cpl: str = Field(default="", max_length=_MAX_TEXT)
    mqls: str = Field(default="", max_length=_MAX_TEXT)
    sqls: str = Field(default="", max_length=_MAX_TEXT)
    last_update: str = Field(default="", max_length=_MAX_TEXT)
    status: str = Field(default="", max_length=_MAX_TEXT)
    report_link: str = Field(default="", max_length=_MAX_TEXT)
    tracking_detail: str = Field(default="", max_length=_MAX_TEXT)
    # The HubSpot campaign it takes its leads, MQLs and SQLs from, and how it was linked
    hubspot_campaign_id: str | None = Field(default=None, max_length=64)
    hubspot_link: HubSpotLink | None = None
    # Leads from that campaign, to date (filled by Sync HubSpot; not a sheet column)
    leads: str | None = Field(default=None, max_length=20)


class PaidSearchBoardSave(BaseModel):
    """The whole board, in order. Saving it replaces what was saved before."""

    entries: list[PaidSearchEntry] = Field(max_length=MAX_ENTRIES)


class PaidSearchBoardSaved(BaseModel):
    saved: int
