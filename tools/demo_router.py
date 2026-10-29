from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from models.demo_model import DemoCreate, DemoItem, DemoSummary, DemoUpdate
from services.shared.auth_service import require_permission
from services.shared.demo_service import DemoNotFoundError, DemoValidationError, InitiativeNotFoundError
from services.shared.demo_service import create_demo as insert_demo
from services.shared.demo_service import delete_demo as remove_demo
from services.shared.demo_service import get_demo_summary as fetch_demo_summary
from services.shared.demo_service import list_demos as fetch_demos
from services.shared.demo_service import update_demo as save_demo_changes

router = APIRouter()


def _unprocessable(exc: DemoValidationError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


@router.get(
    "/initiatives/{initiative_id}/demos",
    response_model=list[DemoItem],
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def list_demos(initiative_id: UUID):
    """The demos given at an event, newest first."""
    try:
        return await fetch_demos(initiative_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Initiative not found") from exc


@router.get(
    "/initiatives/{initiative_id}/demos/summary",
    response_model=DemoSummary,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def get_demo_summary(initiative_id: UUID):
    """Demo counts by type, outcome, product and staff member, the demo funnel, and cost per demo / customer.

    Costs are the event's expected total (estimates included) in its own currency; other currencies aren't converted.
    """
    try:
        return await fetch_demo_summary(initiative_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Initiative not found") from exc


@router.post("/initiatives/{initiative_id}/demos", response_model=DemoItem, status_code=status.HTTP_201_CREATED)
async def create_demo(
    initiative_id: UUID,
    payload: DemoCreate,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Log a demo. Time defaults to now, brand to the event's, staff member to you, outcome to "No outcome yet"."""
    try:
        return await insert_demo(initiative_id, payload, user_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Initiative not found") from exc
    except DemoValidationError as exc:
        raise _unprocessable(exc) from exc


@router.patch("/demos/{demo_id}", response_model=DemoItem)
async def update_demo(
    demo_id: UUID,
    payload: DemoUpdate,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Change a demo, e.g. record its outcome after the event; only the fields sent are updated."""
    try:
        return await save_demo_changes(demo_id, payload.model_dump(exclude_unset=True), user_id)
    except DemoNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demo not found") from exc
    except DemoValidationError as exc:
        raise _unprocessable(exc) from exc


@router.delete("/demos/{demo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_demo(
    demo_id: UUID,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    try:
        await remove_demo(demo_id, user_id)
    except DemoNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demo not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
