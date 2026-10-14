from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SpendStatus = Literal["ESTIMATED", "COMMITTED", "INVOICED", "PAID"]
PaymentMethod = Literal["AP_INVOICE", "CORPORATE_CARD", "OTHER"]
CostType = Literal["OPEX", "CAPEX"]
SpendSource = Literal["MANUAL", "STAFF_CALCULATOR", "COST_SHEET_UPLOAD", "TRAVEL_FEED", "GOOGLE_ADS", "MICROSOFT_ADS"]
# Sources a person can enter through the API; the rest arrive from imports and feeds
ManualSpendSource = Literal["MANUAL", "STAFF_CALCULATOR"]

_MAX_AMOUNT = 999_999_999_999


class SpendLineItem(BaseModel):
    id: UUID
    initiative_id: UUID
    category_id: int
    category_code: str
    category_name: str
    expense_type_id: int
    expense_type_code: str
    expense_type_name: str
    description: str | None = None
    vendor: str | None = None
    note: str | None = None
    status: SpendStatus
    estimated_amount: float | None = None
    amount: float
    # amount − estimated_amount, once the actual is known (INVOICED or PAID) and there was an estimate
    variance: float | None = None
    currency: str
    incurred_on: date
    invoice_ref: str | None = None
    paid_on: date | None = None
    payment_method: PaymentMethod | None = None
    cost_type: CostType | None = None
    source: SpendSource
    # Why some fields can't change: a feed or import keeps the line up to date, or Finance reconciled it
    lock_reason: Literal["TRAVEL_FEED", "ADS_IMPORT", "RECONCILED"] | None = None
    locked_fields: list[str]
    can_delete: bool
    # False only when every editable field is locked
    editable: bool
    reconciled_month: date | None = None
    created_by_name: str | None = None
    updated_at: datetime


class _SpendLineFields(BaseModel):
    """Shared clean-up for the text fields people type."""

    description: str | None = Field(default=None, max_length=255)
    vendor: str | None = Field(default=None, max_length=255)
    note: str | None = None
    # The supplier's invoice number only; the invoice itself stays with Finance
    invoice_ref: str | None = Field(default=None, max_length=100)

    @field_validator("description", "vendor", "note", "invoice_ref", "currency", mode="before", check_fields=False)
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("currency", check_fields=False)
    @classmethod
    def upper_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @field_validator("amount", check_fields=False)
    @classmethod
    def non_zero_amount(cls, value: float | None) -> float | None:
        if value == 0:
            raise ValueError("Amount can't be zero. Use a negative amount for a refund or credit.")
        return value


class SpendLineCreate(_SpendLineFields):
    category_id: int
    status: SpendStatus = "COMMITTED"
    amount: float = Field(ge=-_MAX_AMOUNT, le=_MAX_AMOUNT)
    estimated_amount: float | None = Field(default=None, ge=-_MAX_AMOUNT, le=_MAX_AMOUNT)
    # Defaults to the initiative's currency
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    incurred_on: date
    paid_on: date | None = None
    payment_method: PaymentMethod | None = None
    # Defaults to the category's default cost type
    cost_type: CostType | None = None
    source: ManualSpendSource = "MANUAL"


class SpendLineUpdate(_SpendLineFields):
    """Partial update: only the fields sent change. Where a cost came from can't be changed."""

    model_config = ConfigDict(extra="forbid")

    category_id: int | None = None
    status: SpendStatus | None = None
    amount: float | None = Field(default=None, ge=-_MAX_AMOUNT, le=_MAX_AMOUNT)
    estimated_amount: float | None = Field(default=None, ge=-_MAX_AMOUNT, le=_MAX_AMOUNT)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    incurred_on: date | None = None
    paid_on: date | None = None
    payment_method: PaymentMethod | None = None
    cost_type: CostType | None = None

    @model_validator(mode="after")
    def check_required_fields(self) -> "SpendLineUpdate":
        # These can be changed but not cleared
        labels = {"category_id": "Category", "status": "Status", "amount": "Amount", "currency": "Currency", "incurred_on": "Date"}
        for field, label in labels.items():
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{label} can't be empty")
        return self


# --- Cost summary for one initiative (amounts in the initiative's currency) ---

class SpendFigures(BaseModel):
    expected_total: float
    committed: float
    actual: float


class SpendCategorySummary(SpendFigures):
    category_id: int
    name: str
    estimated: float
    # Actual − estimate, for invoiced / paid lines that had an estimate
    variance: float
    line_count: int


class SpendExpenseTypeSummary(SpendFigures):
    expense_type_id: int
    code: str
    name: str
    source: Literal["PORTAL", "TRAVEL_SYSTEM"]
    estimated: float
    variance: float
    # Share of the initiative's expected total
    percent_of_total: float | None = None
    categories: list[SpendCategorySummary]


class AwaitingInvoice(BaseModel):
    """COMMITTED costs whose date has passed: invoices that are probably late."""

    count: int
    amount: float


class SpendTotals(SpendFigures):
    paid: float
    still_estimated: float
    # Budget − expected total; negative means it's expected to go over
    forecast_remaining: float | None = None
    remaining_committed: float | None = None
    percent_expected: float | None = None
    percent_committed: float | None = None
    percent_actual: float | None = None
    awaiting_invoice: AwaitingInvoice


class SpendBySource(BaseModel):
    supplier: float
    travel: float


class SpendMonth(SpendFigures):
    month: date


class SpendOtherCurrency(SpendFigures):
    currency: str


class SpendSummary(BaseModel):
    budget_amount: float | None = None
    budget_currency: str
    totals: SpendTotals
    by_expense_type: list[SpendExpenseTypeSummary]
    by_source: SpendBySource
    by_month: list[SpendMonth]
    # Expected total ÷ the event's number of days; null without both dates
    per_day: float | None = None
    # Expected total ÷ booth size, for comparing in-person events; null without a booth size
    per_sqm: float | None = None
    # Costs in other currencies, reported separately and never converted
    other_currencies: list[SpendOtherCurrency]
    mixed_currencies: bool
