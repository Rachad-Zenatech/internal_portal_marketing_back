"""Dashboard responses from the company's read-only Google marketing reader."""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class GoogleReportingBrand(BaseModel):
    code: str
    name: str
    has_ads: bool


class GoogleReportSection(BaseModel):
    status: Literal["ok", "empty", "unavailable", "error"]
    rows: list[dict[str, str | int | float]] = Field(default_factory=list)
    totals: dict[str, int | float] = Field(default_factory=dict)
    message: str | None = None
    time_zone: str | None = None
    currency: str | None = None


class GoogleMarketingReport(BaseModel):
    brand: GoogleReportingBrand
    date_from: date
    date_to: date
    fetched_at: datetime
    ga4: GoogleReportSection
    search_console: GoogleReportSection
    ads: GoogleReportSection
