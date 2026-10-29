from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from models.spend_model import SpendLineCreate, SpendLineItem, SpendLineUpdate, SpendSummary
from services.shared.auth_service import require_permission
from services.shared.spend_service import (
    InitiativeNotFoundError,
    SpendLineNotFoundError,
    SpendValidationError,
)
from services.shared.spend_service import create_spend_line as insert_spend_line
from services.shared.spend_service import delete_spend_line as remove_spend_line
from services.shared.spend_service import get_spend_summary as fetch_spend_summary
from services.shared.spend_service import list_spend_lines as fetch_spend_lines
from services.shared.spend_service import update_spend_line as save_spend_line_changes

router = APIRouter()


def _unprocessable(exc: SpendValidationError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


@router.get(
    "/initiatives/{initiative_id}/spend-lines",
    response_model=list[SpendLineItem],
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def list_spend_lines(initiative_id: UUID):
    """An initiative's costs, grouped by expense type and category, then by date."""
    try:
        return await fetch_spend_lines(initiative_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Initiative not found") from exc


@router.get(
    "/initiatives/{initiative_id}/spend-summary",
    response_model=SpendSummary,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def get_spend_summary(initiative_id: UUID):
    """Expected, committed, invoiced and paid costs against the budget, by expense type and category.

    Amounts are in the initiative's currency; costs in other currencies are listed separately, never converted.
    """
    try:
        return await fetch_spend_summary(initiative_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Initiative not found") from exc


@router.post("/initiatives/{initiative_id}/spend-lines", response_model=SpendLineItem, status_code=status.HTTP_201_CREATED)
async def create_spend_line(
    initiative_id: UUID,
    payload: SpendLineCreate,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Add a cost. Currency defaults to the initiative's; cost type to the category's."""
    try:
        return await insert_spend_line(initiative_id, payload, user_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Initiative not found") from exc
    except SpendValidationError as exc:
        raise _unprocessable(exc) from exc


@router.patch("/spend-lines/{line_id}", response_model=SpendLineItem)
async def update_spend_line(
    line_id: UUID,
    payload: SpendLineUpdate,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Change a cost; only the fields sent are updated."""
    try:
        return await save_spend_line_changes(line_id, payload.model_dump(exclude_unset=True), user_id)
    except SpendLineNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cost not found") from exc
    except SpendValidationError as exc:
        raise _unprocessable(exc) from exc


@router.delete("/spend-lines/{line_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_spend_line(
    line_id: UUID,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    try:
        await remove_spend_line(line_id, user_id)
    except SpendLineNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cost not found") from exc
    except SpendValidationError as exc:
        raise _unprocessable(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
