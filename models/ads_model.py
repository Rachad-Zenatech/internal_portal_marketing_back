"""Paid ads (every Paid Search and Paid Social platform): results, the page overview, and the spreadsheet upload."""
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AdPlatform = Literal[
    "GOOGLE_ADS", "BING_ADS", "LINKEDIN_ADS", "META_ADS", "YOUTUBE_ADS", "TIKTOK_ADS", "X_ADS", "REDDIT_ADS",
    "DISPLAY_PROGRAMMATIC", "OTHER",
]
AdResultStatus = Literal["ACTIVE", "PAUSED", "ENDED"]
AdResultSource = Literal["MANUAL", "SPREADSHEET", "DATA_STUDIO", "GOOGLE_ADS_API"]
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
    platform: AdPlatform | Literal["ALL"]
    currency: str
    campaign_count: int


class AdResultList(BaseModel):
    results: list[AdResult]
    # Per platform and currency, then ALL per currency
    totals: list[AdTotals]


class AdCampaign(BaseModel):
    """An ad campaign (Paid Search or Paid Social initiative with a platform), for pickers."""

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


# --- The Paid Advertising page (services/ads_overview_service.py) ---

CampaignObjectiveCode = Literal["AWARENESS", "TRAFFIC", "LEADS", "CONVERSIONS", "ENGAGEMENT", "VIDEO_VIEWS"]
InitiativeStatusCode = Literal["DRAFT", "PLANNED", "ACTIVE", "COMPLETED", "PAUSED", "CANCELLED"]
AttentionKind = Literal["BUDGET", "CPL_ABOVE_TARGET", "CPL_ABOVE_PLATFORM", "UNEXPECTEDLY_PAUSED", "DATA_DELAYED"]
DataHealthStatus = Literal["OK", "STALE", "NO_DATA", "CONNECTION", "NOT_STARTED"]
DataSourceKind = Literal["API", "MANUAL", "SPREADSHEET", "DATA_STUDIO", "GOOGLE_ADS_API"]


class AdsKpis(BaseModel):
    """Key figures for a date range in one currency. Verified leads and platform conversions are kept apart."""

    allocated_budget: float | None = None
    actual_spend: float | None = None
    budget_remaining: float | None = None
    remaining_percent: float | None = None
    verified_leads: float | None = None
    cost_per_lead: float | None = None
    conversions: float | None = None
    impressions: float | None = None
    clicks: float | None = None
    ctr: float | None = None
    cpc: float | None = None
    mqls: float | None = None
    sqls: float | None = None
    campaign_count: int
    active_campaign_count: int


class AdsPlatformRow(BaseModel):
    platform: AdPlatform
    campaign_count: int
    allocated_budget: float | None = None
    spend: float | None = None
    impressions: float | None = None
    clicks: float | None = None
    ctr: float | None = None
    cpc: float | None = None
    conversions: float | None = None
    verified_leads: float | None = None
    cpl: float | None = None
    mqls: float | None = None
    sqls: float | None = None
    budget_used_percent: float | None = None
    previous_spend: float | None = None
    previous_verified_leads: float | None = None
    previous_cpl: float | None = None


class AdsBrandRow(BaseModel):
    brand_id: int
    brand_name: str
    campaign_count: int
    allocated_budget: float | None = None
    spend: float | None = None
    budget_used_percent: float | None = None
    verified_leads: float | None = None
    cpl: float | None = None


class AdsMonthRow(BaseModel):
    month: date
    # The part of the month inside the date range
    start: date
    end: date
    allocated_budget: float | None = None
    spend: float | None = None
    verified_leads: float | None = None


class AdsTimePoint(BaseModel):
    start: date
    end: date
    spend: float
    verified_leads: float
    conversions: float
    impressions: float
    clicks: float


class AdsAttentionReason(BaseModel):
    kind: AttentionKind
    severity: Literal["HIGH", "MEDIUM"]
    # Budget: percent used; cost per lead: the campaign's cost per lead
    value: float | None = None
    # Budget: the alert percent; cost per lead: the target or the platform's cost per lead
    benchmark: float | None = None


class AdsAttentionItem(AdsAttentionReason):
    initiative_id: UUID
    name: str
    platform: AdPlatform
    brand_name: str


class AdsPrimaryKpi(BaseModel):
    code: str
    name: str
    unit: Literal["COUNT", "MONEY", "PERCENT"]


class AdsSuccessMetric(BaseModel):
    """The figure for the campaign's objective (CPM, CPC, CPE, CPL or CPA), and its target when the KPI is the same."""

    code: Literal["CPM", "CPC", "CPE", "CPL", "CPA"]
    label: str
    value: float | None = None
    target: float | None = None
    on_target: bool | None = None


class AdsDataHealth(BaseModel):
    status: DataHealthStatus
    source: DataSourceKind | None = None
    last_data_date: date | None = None
    # The platform's API connection, when there is one
    connection_status: str | None = None


class AdsCampaignRow(BaseModel):
    initiative_id: UUID
    name: str
    description: str | None = None
    brand_id: int
    brand_name: str
    market_id: int | None = None
    market_name: str | None = None
    platform: AdPlatform
    channel: Literal["PAID_SEARCH", "PAID_SOCIAL"]
    objective: str | None = None
    ad_account_id: str | None = None
    status: InitiativeStatusCode
    start_date: date | None = None
    end_date: date | None = None
    currency: str
    owner_name: str | None = None
    data_source: str
    # The campaign's budget (approved, else the budgets on its results) and spend since it started
    budget_amount: float | None = None
    spend_to_date: float | None = None
    budget_used_percent: float | None = None
    # The date range
    allocated_budget: float | None = None
    spend: float | None = None
    impressions: float | None = None
    clicks: float | None = None
    conversions: float | None = None
    verified_leads: float | None = None
    mqls: float | None = None
    sqls: float | None = None
    ctr: float | None = None
    cpc: float | None = None
    cpm: float | None = None
    conversion_rate: float | None = None
    cpl: float | None = None
    cost_per_mql: float | None = None
    cost_per_conversion: float | None = None
    revenue_to_date: float | None = None
    roas: float | None = None
    primary_kpi: AdsPrimaryKpi | None = None
    kpi_target: float | None = None
    # Costs and rates for the date range; counts since the campaign started
    primary_kpi_value: float | None = None
    primary_kpi_on_target: bool | None = None
    success_metric: AdsSuccessMetric
    report_url: str | None = None
    result_count: int
    data_health: AdsDataHealth
    attention: list[AdsAttentionReason]


class AdsOverview(BaseModel):
    date_from: date
    date_to: date
    previous_from: date
    previous_to: date
    today: date
    currency: str
    # Currencies of the campaigns matching the filters, most used first
    currencies: list[str]
    kpis: AdsKpis
    previous: AdsKpis
    by_platform: list[AdsPlatformRow]
    by_brand: list[AdsBrandRow]
    by_month: list[AdsMonthRow]
    bucket: Literal["day", "week", "month"]
    timeseries: list[AdsTimePoint]
    attention: list[AdsAttentionItem]
    campaigns: list[AdsCampaignRow]


class AdsCampaignBudget(BaseModel):
    budget_amount: float | None = None
    spent: float | None = None
    remaining: float | None = None
    budget_used_percent: float | None = None
    time_elapsed_percent: float | None = None
    status: Literal["NOT_STARTED", "AHEAD", "ON_TRACK", "BEHIND"] | None = None
    days_total: int | None = None
    days_elapsed: int | None = None


class AdsCampaignCrm(BaseModel):
    """Since the campaign started: leads, MQLs and SQLs from its results; demos, customers, revenue as recorded."""

    verified_leads: float | None = None
    mqls: float | None = None
    sqls: float | None = None
    demos: float | None = None
    customers: float | None = None
    revenue: float | None = None
    spend: float | None = None
    cost_per_lead: float | None = None
    cost_per_sql: float | None = None
    roas: float | None = None


class AdsCampaignFunnel(BaseModel):
    impressions: float | None = None
    clicks: float | None = None
    conversions: float | None = None
    verified_leads: float | None = None
    mqls: float | None = None
    sqls: float | None = None


class AdsCampaignLink(BaseModel):
    system: str
    external_id: str
    account_id: str | None = None


class AdsCampaignDetail(BaseModel):
    campaign: AdsCampaignRow
    currency: str
    date_from: date
    date_to: date
    bucket: Literal["day", "week", "month"]
    trend: list[AdsTimePoint]
    funnel: AdsCampaignFunnel
    # The platform's cost per lead in the range (same currency), to compare with
    platform_cpl: float | None = None
    budget: AdsCampaignBudget
    crm: AdsCampaignCrm
    links: list[AdsCampaignLink]


class AdsHealthProblem(BaseModel):
    initiative_id: UUID
    name: str
    brand_name: str
    status: str
    health: DataHealthStatus
    last_data_date: date | None = None


class AdsHealthConnection(BaseModel):
    account_name: str | None = None
    account_id: str | None = None
    status: str
    updated_at: datetime


class AdsHealthPlatform(BaseModel):
    platform: AdPlatform
    connection_status: str | None = None
    connections: list[AdsHealthConnection]
    source: DataSourceKind | None = None
    # Ad account ids entered on its campaigns
    accounts: list[str]
    campaign_count: int
    active_campaign_count: int
    last_data_date: date | None = None
    last_updated_at: datetime | None = None
    # When the Google Ads sync last worked, and in which mode ("mock" or "live"); Google Ads and YouTube Ads only
    last_sync_at: datetime | None = None
    sync_mode: str | None = None
    problems: list[AdsHealthProblem]


class AdsHealth(BaseModel):
    today: date
    stale_after_days: int
    platforms: list[AdsHealthPlatform]
