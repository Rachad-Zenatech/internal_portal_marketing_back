from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from models.budget_model import (
    BudgetCopy,
    BudgetCreate,
    BudgetDetail,
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
from services import budget_service
from services.auth_service import require_permission
from services.budget_service import BudgetNotFoundError, BudgetPermissionError, BudgetValidationError

router = APIRouter()

_VIEW = require_permission("INITIATIVES_VIEW")
_UPDATE = require_permission("INITIATIVES_UPDATE")


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
