"""Monthly budgets, money moves between them, and comments."""
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

BudgetStatus = Literal["APPROVED", "NOT_COMMITTED", "CANCELLED"]
BudgetProgress = Literal["CANCELLED", "NOT_COMMITTED", "COMPLETE", "IN_PROGRESS", "NOT_STARTED"]

_MAX_AMOUNT = 999_999_999_999


def _first_of_month(value: date | None) -> date | None:
    if value is not None and value.day != 1:
        raise ValueError("Use the first day of the month, e.g. 2026-09-01.")
    return value


def _upper_currency(value: str | None) -> str | None:
    return value.upper() if value else value


# --- Reading ---

class Comment(BaseModel):
    id: UUID
    # Empty for a note on the whole month
    budget_id: UUID | None = None
    month: date
    body: str
    author_id: UUID | None = None
    author_name: str | None = None
    created_at: datetime


class LatestComment(BaseModel):
    body: str
    author_name: str | None = None
    created_at: datetime


class CurrencyFigures(BaseModel):
    """Costs in a currency other than the budget's: listed, never converted."""

    currency: str
    spent: float
    expected: float


class BudgetRow(BaseModel):
    id: UUID
    month: date
    # Empty brand = TBD/All; empty channel = all channels
    brand_id: int | None = None
    brand_name: str | None = None
    market_id: int
    market_name: str
    channel_id: int | None = None
    channel_name: str | None = None
    amount: float
    currency: str
    status: BudgetStatus
    progress: BudgetProgress
    moved_in: float
    moved_out: float
    # amount + moved_in - moved_out
    available: float
    # Invoiced and paid costs
    spent: float
    # Every cost, estimates included
    expected: float
    remaining: float
    percent_used: float | None = None
    over_budget: bool
    other_currency: list[CurrencyFigures]
    comment_count: int
    latest_comment: LatestComment | None = None
    last_update: datetime
    last_update_by_name: str | None = None


class BudgetTotals(BaseModel):
    currency: str
    amount: float
    available: float
    spent: float
    expected: float
    remaining: float


class UnbudgetedSpend(BaseModel):
    """Costs in a month that no budget covers ("Spend without a budget")."""

    brand_id: int
    brand_name: str
    market_id: int
    market_name: str
    channel_id: int
    channel_name: str
    currency: str
    spent: float
    expected: float
    initiative_count: int


class MonthBudgets(BaseModel):
    month: date
    budgets: list[BudgetRow]
    totals: list[BudgetTotals]
    unbudgeted: list[UnbudgetedSpend]
    notes: list[Comment]
    previous_month_has_budgets: bool


class BudgetSpend(BaseModel):
    """Costs counted against a budget, per initiative."""

    initiative_id: UUID
    initiative_name: str
    channel_name: str
    sub_channel_name: str | None = None
    event_code: str | None = None
    currency: str
    spent: float
    expected: float
    line_count: int


class BudgetTransferItem(BaseModel):
    id: UUID
    direction: Literal["IN", "OUT"]
    other_budget_id: UUID
    # e.g. "Aug 2026 · TBD/All · London"
    other_budget_label: str
    amount: float
    note: str
    created_at: datetime
    created_by_name: str | None = None


class BudgetDetail(BudgetRow):
    spend: list[BudgetSpend]
    transfers: list[BudgetTransferItem]
    comments: list[Comment]


class BudgetTransfer(BaseModel):
    id: UUID
    from_budget_id: UUID
    to_budget_id: UUID
    amount: float
    note: str
    created_at: datetime
    created_by_name: str | None = None


class CopyResult(BaseModel):
    created: int
    skipped: int


# --- Writing ---

class BudgetCreate(BaseModel):
    month: date
    brand_id: int | None = None
    market_id: int
    channel_id: int | None = None
    amount: float = Field(ge=0, le=_MAX_AMOUNT)
    currency: str = Field(pattern=r"^[A-Za-z]{3}$")
    status: BudgetStatus = "APPROVED"

    @field_validator("month")
    @classmethod
    def check_month(cls, value: date | None) -> date | None:
        return _first_of_month(value)

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str | None) -> str | None:
        return _upper_currency(value)


class BudgetUpdate(BaseModel):
    """Partial update: only the fields sent change. Send null brand or channel for TBD/All or all channels."""

    brand_id: int | None = None
    market_id: int | None = None
    channel_id: int | None = None
    amount: float | None = Field(default=None, ge=0, le=_MAX_AMOUNT)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    status: BudgetStatus | None = None

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str | None) -> str | None:
        return _upper_currency(value)

    @model_validator(mode="after")
    def check_required_fields(self) -> "BudgetUpdate":
        labels = {"market_id": "Market", "amount": "Amount", "currency": "Currency", "status": "Status"}
        for field, label in labels.items():
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{label} can't be empty.")
        return self


class BudgetCopy(BaseModel):
    from_month: date
    to_month: date

    @field_validator("from_month", "to_month")
    @classmethod
    def check_months(cls, value: date) -> date:
        return _first_of_month(value)


class BudgetTransferCreate(BaseModel):
    from_budget_id: UUID
    to_budget_id: UUID
    amount: float = Field(gt=0, le=_MAX_AMOUNT)
    note: str = Field(max_length=2000)

    @field_validator("note")
    @classmethod
    def check_note(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Add a note saying why the money is moving.")
        return value


class CommentCreate(BaseModel):
    # One of these: a budget (its month is used), or a month for a note on the whole month
    budget_id: UUID | None = None
    month: date | None = None
    body: str

    @field_validator("month")
    @classmethod
    def check_month(cls, value: date | None) -> date | None:
        return _first_of_month(value)

    @field_validator("body")
    @classmethod
    def check_body(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Write a comment first.")
        if len(value) > 2000:
            raise ValueError("Keep comments to 2000 characters or fewer.")
        return value

    @model_validator(mode="after")
    def check_target(self) -> "CommentCreate":
        if self.budget_id is None and self.month is None:
            raise ValueError("Choose a budget or a month for the comment.")
        return self


# --- Week view ---

class WeekLine(BaseModel):
    """One cost on the day it's dated, with the budget it counts against (none if no budget covers it)."""

    id: UUID
    incurred_on: date
    description: str | None = None
    vendor: str | None = None
    status: str
    amount: float
    currency: str
    category_name: str | None = None
    initiative_id: UUID
    initiative_name: str
    event_code: str | None = None
    brand_name: str
    market_name: str
    channel_name: str
    budget_id: UUID | None = None
    # e.g. "Oct 2026 · DeskFlex · North America · Paid Search"
    budget_label: str | None = None


class WeekTotals(BaseModel):
    currency: str
    spent: float
    expected: float


class WeekMonthPace(BaseModel):
    """A month the week falls in: what its budgets have, and what they'd spent by the end of the week."""

    month: date
    currency: str
    available: float
    spent_to_date: float
    # The last day counted: the week's Sunday, or the month's last day if the week runs into the next month
    through: date
    days_in_month: int


class BudgetWeek(BaseModel):
    start: date
    end: date
    lines: list[WeekLine]
    totals: list[WeekTotals]
    months: list[WeekMonthPace]
