from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status

from models.budgets.budget_model import (
    BudgetCopy,
    BudgetCreate,
    BudgetDetail,
    BudgetExpenses,
    BudgetImportConfirmResult,
    BudgetImportPreview,
    BudgetImportUndoResult,
    BudgetPeriod,
    BudgetRow,
    BudgetTransfer,
    BudgetTransferCreate,
    BudgetUpdate,
    BudgetWeek,
    Comment,
    CommentCreate,
    CopyResult,
    MonthBudgets,
)
from services.budgets import budget_import_service, budget_service
from services.shared.auth_service import require_permission
from services.budgets.budget_service import BudgetNotFoundError, BudgetPermissionError, BudgetValidationError
from services.shared.cost_sheet_import_service import (
    ImportConflictError,
    ImportExpiredError,
    ImportForbiddenError,
    ImportNotFoundError,
)
from services.shared.import_parsing import ImportFileError

router = APIRouter()

_VIEW = require_permission("INITIATIVES_VIEW")
_UPDATE = require_permission("INITIATIVES_UPDATE")
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _unprocessable(exc: BudgetValidationError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


@router.get("/budgets", response_model=MonthBudgets, dependencies=[Depends(_VIEW)])
async def get_month(month: date):
    """A month's budgets with what's been spent against each, totals per currency and spend without a budget."""
    if month.day != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Use the first day of the month, e.g. 2026-09-01.",
        )
    return await budget_service.get_month(month)


@router.post("/budgets", response_model=BudgetRow, status_code=status.HTTP_201_CREATED)
async def create_budget(payload: BudgetCreate, user_id: Annotated[UUID, Depends(_UPDATE)]):
    try:
        return await budget_service.create_budget(payload.model_dump(), user_id)
    except BudgetValidationError as exc:
        raise _unprocessable(exc) from exc


# Declared before /budgets/{budget_id} so "copy" isn't read as an id
@router.post("/budgets/copy", response_model=CopyResult)
async def copy_month(payload: BudgetCopy, user_id: Annotated[UUID, Depends(_UPDATE)]):
    """Copy a month's budgets into another month as Not committed, skipping ones that already exist there."""
    try:
        return await budget_service.copy_month(payload.from_month, payload.to_month, user_id)
    except BudgetValidationError as exc:
        raise _unprocessable(exc) from exc


# Declared before /budgets/{budget_id} so "week" isn't read as an id
@router.get("/budgets/week", response_model=BudgetWeek, dependencies=[Depends(_VIEW)])
async def get_week(start: date):
    """A Monday-to-Sunday week of costs, each on its day with the budget it counts against."""
    if start.weekday() != 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Start the week on a Monday, e.g. 2026-10-12.",
        )
    return await budget_service.get_week(start)


MAX_PERIOD_MONTHS = 24


def _check_period(start: date, end: date, months: bool) -> None:
    if start > end:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The start of the period can't be after its end.")
    span = (end.year - start.year) * 12 + end.month - start.month + 1
    if span > MAX_PERIOD_MONTHS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Choose a period of at most {MAX_PERIOD_MONTHS} months."
        )
    if months and (start.day != 1 or end.day != 1):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Use the first day of each month, e.g. 2026-10-01."
        )


# Declared before /budgets/{budget_id} so "period" and "expenses" aren't read as ids
@router.get("/budgets/period", response_model=BudgetPeriod, dependencies=[Depends(_VIEW)])
async def get_period(
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
):
    """Budgets from one month to another (first days of the months), with spent, committed and remaining, totals per
    currency, figures by month, spend by channel, spend without a budget and travel outside budgets."""
    _check_period(date_from, date_to, months=True)
    return await budget_service.get_period(date_from, date_to)


@router.get("/budgets/expenses", response_model=BudgetExpenses, dependencies=[Depends(_VIEW)])
async def list_expenses(
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
):
    """Every cost dated from one day to another, with its source, stage, reconciliation and the budget it counts against."""
    _check_period(date_from, date_to, months=False)
    return await budget_service.list_expenses(date_from, date_to)


@router.get("/budgets/{budget_id}", response_model=BudgetDetail, dependencies=[Depends(_VIEW)])
async def get_budget(budget_id: UUID):
    try:
        return await budget_service.get_budget(budget_id)
    except BudgetNotFoundError as exc:
        raise _not_found("Budget") from exc


@router.patch("/budgets/{budget_id}", response_model=BudgetRow)
async def update_budget(budget_id: UUID, payload: BudgetUpdate, user_id: Annotated[UUID, Depends(_UPDATE)]):
    try:
        return await budget_service.update_budget(budget_id, payload.model_dump(exclude_unset=True), user_id)
    except BudgetNotFoundError as exc:
        raise _not_found("Budget") from exc
    except BudgetValidationError as exc:
        raise _unprocessable(exc) from exc


@router.delete("/budgets/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(budget_id: UUID, user_id: Annotated[UUID, Depends(_UPDATE)]):
    try:
        await budget_service.delete_budget(budget_id, user_id)
    except BudgetNotFoundError as exc:
        raise _not_found("Budget") from exc
    except BudgetValidationError as exc:
        raise _unprocessable(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/budget-transfers", response_model=BudgetTransfer, status_code=status.HTTP_201_CREATED)
async def create_transfer(payload: BudgetTransferCreate, user_id: Annotated[UUID, Depends(_UPDATE)]):
    """Move money between two budgets in the same currency, e.g. carryover or borrowing. A note is required."""
    try:
        return await budget_service.create_transfer(payload.model_dump(), user_id)
    except BudgetValidationError as exc:
        raise _unprocessable(exc) from exc


@router.delete("/budget-transfers/{transfer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transfer(transfer_id: UUID, user_id: Annotated[UUID, Depends(_UPDATE)]):
    try:
        await budget_service.delete_transfer(transfer_id, user_id)
    except BudgetNotFoundError as exc:
        raise _not_found("Money move") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/budget-comments", response_model=Comment, status_code=status.HTTP_201_CREATED)
async def create_comment(payload: CommentCreate, user_id: Annotated[UUID, Depends(_VIEW)]):
    """Comment on a budget, or on a whole month (send month without budget_id)."""
    try:
        return await budget_service.create_comment(payload.model_dump(), user_id)
    except BudgetNotFoundError as exc:
        raise _not_found("Budget") from exc


@router.delete("/budget-comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(comment_id: UUID, user_id: Annotated[UUID, Depends(_VIEW)]):
    try:
        await budget_service.delete_comment(comment_id, user_id)
    except BudgetNotFoundError as exc:
        raise _not_found("Comment") from exc
    except BudgetPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Budget tracker upload ---

@router.post("/budget-imports", response_model=BudgetImportPreview, status_code=status.HTTP_201_CREATED)
async def upload_budget_sheet(
    user_id: Annotated[UUID, Depends(_UPDATE)],
    file: UploadFile = File(...),
    year: int = Form(ge=2000, le=2100),
    currency: str = Form(default=budget_import_service.DEFAULT_CURRENCY),
):
    """Check a budget tracker (.xlsx or .csv) and show what would be imported. Nothing is saved until confirmed.

    `year` is used for months written without one (e.g. "Aug"); `currency` for amounts without a symbol or code.
    """
    data = await file.read(budget_import_service.MAX_FILE_BYTES + 1)
    if len(data) > budget_import_service.MAX_FILE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="The file is larger than 5 MB.")
    try:
        return await budget_import_service.preview_budget_sheet(file.filename or "upload", data, user_id, year, currency)
    except ImportFileError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/budget-imports/{batch_id}/confirm", response_model=BudgetImportConfirmResult)
async def confirm_budget_import(batch_id: UUID, user_id: Annotated[UUID, Depends(_UPDATE)]):
    """Add and update the previewed budgets, with each row's note as a comment."""
    try:
        return await budget_import_service.confirm_import(batch_id, user_id)
    except ImportNotFoundError as exc:
        raise _not_found("Upload") from exc
    except ImportForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the person who uploaded this file can confirm it.") from exc
    except ImportConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ImportExpiredError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This preview has expired. Upload the file again.") from exc


@router.post("/budget-imports/{batch_id}/undo", response_model=BudgetImportUndoResult)
async def undo_budget_import(batch_id: UUID, user_id: Annotated[UUID, Depends(_UPDATE)]):
    """Remove the budgets and notes the upload added and give the budgets it changed their previous values back."""
    try:
        return await budget_import_service.undo_import(batch_id, user_id)
    except ImportNotFoundError as exc:
        raise _not_found("Upload") from exc
    except ImportConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/budget-imports/template", dependencies=[Depends(_VIEW)], response_class=Response)
async def download_budget_template():
    """An .xlsx with the same columns as Marketing's Budget Tracker tab."""
    return Response(
        content=await budget_import_service.build_template(),
        media_type=_XLSX,
        headers={"Content-Disposition": 'attachment; filename="budget-tracker-template.xlsx"'},
    )
