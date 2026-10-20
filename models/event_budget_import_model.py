"""Uploading many events' budgets from one spreadsheet (services/event_budget_import_service.py)."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

_MAX_AMOUNT = 999_999_999_999


class EventBudgetPreviewRow(BaseModel):
    row_number: int
    event_text: str
    status: Literal["UPDATE", "UNCHANGED", "ERROR"]
    message: str | None = None
    initiative_id: UUID | None = None
    event_name: str | None = None
    event_code: str | None = None
    event_status: str | None = None
    old_budget: float | None = None
    new_budget: float | None = None
    old_currency: str | None = None
    new_currency: str | None = None
    old_travel_budget: float | None = None
    new_travel_budget: float | None = None


class EventBudgetPreview(BaseModel):
    rows: list[EventBudgetPreviewRow]
    counts: dict[str, int]
    ignored_columns: list[str]


class EventBudgetChange(BaseModel):
    initiative_id: UUID
    budget_amount: float = Field(ge=0, le=_MAX_AMOUNT)
    currency: str = Field(pattern=r"^[A-Za-z]{3}$")
    travel_budget_amount: float | None = Field(default=None, ge=0, le=_MAX_AMOUNT)

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str) -> str:
        return value.upper()


class EventBudgetApply(BaseModel):
    changes: list[EventBudgetChange] = Field(min_length=1, max_length=5000)


class EventBudgetApplyResult(BaseModel):
    updated: int
