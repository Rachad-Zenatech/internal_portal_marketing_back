from datetime import date as Date
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

ImportRowStatus = Literal["READY", "READY_WITH_WARNINGS", "DUPLICATE", "INVALID"]
ImportBatchStatus = Literal["PREVIEW", "CONFIRMED", "UNDONE", "EXPIRED"]


class ImportCounts(BaseModel):
    total: int
    # READY + READY_WITH_WARNINGS: what confirming would import
    ready: int
    warnings: int
    duplicates: int
    invalid: int


class CostSheetPreviewRow(BaseModel):
    """One sheet row as it would be imported, with every problem found."""

    row_number: int
    status: ImportRowStatus
    messages: list[str]
    item: str | None = None
    category: str | None = None
    expense_type: str | None = None
    vendor: str | None = None
    estimate: float | None = None
    actual: float | None = None
    status_value: str | None = None
    invoice_ref: str | None = None
    date: Date | None = None
    paid_on: Date | None = None
    currency: str | None = None


class TotalsPreview(BaseModel):
    """What the rows that would be imported add up to, in the initiative's currency."""

    expected_total: float
    committed: float
    actual: float


class CostSheetPreview(BaseModel):
    batch_id: UUID
    filename: str
    counts: ImportCounts
    # Sheet columns nothing reads, e.g. "Notes 2"
    ignored_columns: list[str]
    rows: list[CostSheetPreviewRow]
    totals_preview: TotalsPreview


class ImportConfirmResult(BaseModel):
    batch_id: UUID
    status: ImportBatchStatus
    imported_rows: int
    # Rows whose supplier invoice was recorded by someone else between preview and confirm
    skipped: int


class ImportUndoResult(BaseModel):
    batch_id: UUID
    status: ImportBatchStatus
    removed_rows: int


class ImportBatchSummary(BaseModel):
    """One upload in an initiative's history."""

    id: UUID
    kind: str
    filename: str
    status: ImportBatchStatus
    total_rows: int
    ready_rows: int
    error_rows: int
    duplicate_rows: int
    imported_rows: int
    created_by_name: str | None = None
    created_at: datetime
    confirmed_at: datetime | None = None
    confirmed_by_name: str | None = None
    undone_at: datetime | None = None
    undone_by_name: str | None = None
    # Lines from this upload still on the initiative, and how many were edited after the import,
    # so the UI can warn that undo removes edited lines too
    line_count: int
    edited_count: int
