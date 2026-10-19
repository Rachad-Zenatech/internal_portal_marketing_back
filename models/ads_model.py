"""Paid ads (Google Ads and Bing Ads): results per campaign or ad group per period, and the spreadsheet upload."""
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AdPlatform = Literal["GOOGLE_ADS", "BING_ADS"]
AdResultStatus = Literal["ACTIVE", "PAUSED", "ENDED"]
AdResultSource = Literal["MANUAL", "SPREADSHEET", "DATA_STUDIO"]
AdImportRowStatus = Literal["NEW", "UPDATE", "UNCHANGED", "INVALID", "NEW_CAMPAIGN"]
AdImportBatchStatus = Literal["PREVIEW", "CONFIRMED", "UNDONE", "EXPIRED"]

_MAX_AMOUNT = 999_999_999_999
_MAX_COUNT = 2_147_483_647
_MAX_BIG_COUNT = 9_000_000_000_000_000


class AdMetrics(BaseModel):
    """The figures typed in, and the ones calculated from them (null when they can't be)."""

    budget_amount: float | None = None
    spend: float | None = None
    impressions: int | None = None
    clicks: int | None = None
    conversions: float | None = None
    hubspot_contacts: int | None = None
    mqls: int | None = None
    sqls: int | None = None
    ctr: float | None = None
    cpc: float | None = None
    conversion_rate: float | None = None
    cpl: float | None = None
    cost_per_mql: float | None = None
    cost_per_sql: float | None = None
    budget_used_percent: float | None = None


class AdResult(AdMetrics):
    id: UUID
    initiative_id: UUID
    # The initiative's name
    campaign_name: str
    platform: AdPlatform
    brand_id: int
    brand_name: str
    market_id: int | None = None
    market_name: str | None = None
    # The initiative's currency
    currency: str
    # Empty = the whole campaign
    ad_group: str | None = None
    period_start: date
    # Empty while it's running
    period_end: date | None = None
    status: AdResultStatus
    report_url: str | None = None
    tracking_detail: str | None = None
    source: AdResultSource
    updated_at: datetime
    updated_by_name: str | None = None
    # After a create or update: ads costs Finance already reconciled, so they were left as they were
    spend_conflicts: int = 0


class AdTotals(AdMetrics):
    platform: Literal["GOOGLE_ADS", "BING_ADS", "ALL"]
    currency: str
    campaign_count: int


class AdResultList(BaseModel):
    results: list[AdResult]
    # Per platform and currency, then ALL per currency
    totals: list[AdTotals]


class AdCampaign(BaseModel):
    """A Google Ads or Bing Ads initiative, for pickers."""

    initiative_id: UUID
    name: str
    platform: AdPlatform
    brand_name: str
    market_name: str | None = None
    currency: str
    status: str
    result_count: int
    last_period_start: date | None = None


class _AdResultFields(BaseModel):
    ad_group: str | None = Field(default=None, max_length=255)
    period_end: date | None = None
    budget_amount: float | None = Field(default=None, ge=0, le=_MAX_AMOUNT)
    spend: float | None = Field(default=None, ge=0, le=_MAX_AMOUNT)
    impressions: int | None = Field(default=None, ge=0, le=_MAX_BIG_COUNT)
    clicks: int | None = Field(default=None, ge=0, le=_MAX_BIG_COUNT)
    conversions: float | None = Field(default=None, ge=0, le=_MAX_AMOUNT)
    hubspot_contacts: int | None = Field(default=None, ge=0, le=_MAX_COUNT)
    mqls: int | None = Field(default=None, ge=0, le=_MAX_COUNT)
    sqls: int | None = Field(default=None, ge=0, le=_MAX_COUNT)
    report_url: str | None = Field(default=None, max_length=2000)
    tracking_detail: str | None = Field(default=None, max_length=2000)

    @field_validator("ad_group", "report_url", "tracking_detail", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("report_url")
    @classmethod
    def https_only(cls, value: str | None) -> str | None:
        if value and not value.lower().startswith("https://"):
            raise ValueError("The report link must start with https://")
        return value


class AdResultCreate(_AdResultFields):
    initiative_id: UUID
    period_start: date
    status: AdResultStatus = "ACTIVE"

    @model_validator(mode="after")
    def check_dates(self) -> "AdResultCreate":
        if self.period_end and self.period_end < self.period_start:
            raise ValueError("The period can't end before it starts.")
        return self


class AdResultUpdate(_AdResultFields):
    """Partial update: only the fields sent change. The campaign can't be changed."""

    model_config = ConfigDict(extra="forbid")

    period_start: date | None = None
    status: AdResultStatus | None = None

    @model_validator(mode="after")
    def check_required_fields(self) -> "AdResultUpdate":
        for field, label in {"period_start": "Period start", "status": "Status"}.items():
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{label} can't be empty")
        return self


# --- Spreadsheet upload ---

class AdImportRow(BaseModel):
    """One sheet row as it would be imported, with every problem found."""

    row_number: int
    status: AdImportRowStatus
    messages: list[str]
    campaign_name: str | None = None
    ad_group: str | None = None
    platform: AdPlatform | None = None
    brand: str | None = None
    market: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    budget_amount: float | None = None
    spend: float | None = None
    impressions: int | None = None
    clicks: int | None = None
    conversions: float | None = None
    hubspot_contacts: int | None = None
    mqls: int | None = None
    sqls: int | None = None
    # The campaign's status on the row (the row check is `status`)
    result_status: AdResultStatus | None = None
    # The matched campaign's currency, or USD for a new campaign
    currency: str | None = None
    report_url: str | None = None
    # The matched campaign; empty for a new one
    initiative_id: UUID | None = None


class AdImportCounts(BaseModel):
    new: int
    update: int
    unchanged: int
    invalid: int
    new_campaign: int


class AdImportPreview(BaseModel):
    batch_id: UUID
    filename: str
    rows: list[AdImportRow]
    counts: AdImportCounts


class AdImportConfirmResult(BaseModel):
    batch_id: UUID
    status: AdImportBatchStatus
    created: int
    updated: int
    unchanged: int
    campaigns_created: int
    # Rows someone changed between preview and confirm in a way that no longer fits (e.g. a campaign removed)
    skipped: int
    # Ads costs Finance already reconciled, so they were left as they were
    spend_conflicts: int = 0


class AdImportUndoResult(BaseModel):
    batch_id: UUID
    status: AdImportBatchStatus
    # Rows the upload created that were removed, and rows it updated that got their previous values back
    removed: int
    restored: int
    # Rows left as they are: changed by a later upload, or their cost is already reconciled
    skipped: int
    campaigns_removed: int
