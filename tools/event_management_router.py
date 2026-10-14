from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from models.event_management_model import (
    EventOverviewItem,
    StaffAssignment,
    StaffAssignmentCreate,
    StaffAssignmentUpdate,
)
from services.auth_service import require_permission
from services.event_management_service import (
    EventManagementValidationError,
    InitiativeNotFoundError,
    StaffAssignmentNotFoundError,
)
from services.event_management_service import add_staff as insert_staff
from services.event_management_service import list_event_overview as fetch_overview
from services.event_management_service import list_staff as fetch_staff
from services.event_management_service import remove_staff as delete_staff
from services.event_management_service import update_staff as save_staff_changes

router = APIRouter()


def _unprocessable(exc: EventManagementValidationError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


@router.get(
    "/events/overview",
    response_model=list[EventOverviewItem],
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def list_event_overview(
    search: str | None = Query(None, max_length=200),
    timing: Literal["NOW", "UPCOMING", "PAST", "UNSCHEDULED"] | None = None,
):
    """Every event with what still needs doing — staff, travel details, costs, demos, results.

    On now first, then upcoming (soonest first), then past (most recent first).
    """
    return await fetch_overview(search, timing)


@router.get(
    "/initiatives/{initiative_id}/staff",
    response_model=list[StaffAssignment],
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def list_staff(initiative_id: UUID):
    """The team going to an event, with what the travel system still needs to book each person."""
    try:
        return await fetch_staff(initiative_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found") from exc


@router.post("/initiatives/{initiative_id}/staff", response_model=StaffAssignment, status_code=status.HTTP_201_CREATED)
async def add_staff(
    initiative_id: UUID,
    payload: StaffAssignmentCreate,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Put someone on an event's team. Without dates, they arrive the day before it starts and leave on the last day."""
    try:
        return await insert_staff(initiative_id, payload, user_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found") from exc
    except EventManagementValidationError as exc:
        raise _unprocessable(exc) from exc


@router.patch("/event-staff/{assignment_id}", response_model=StaffAssignment)
async def update_staff(
    assignment_id: UUID,
    payload: StaffAssignmentUpdate,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    try:
        return await save_staff_changes(assignment_id, payload.model_dump(exclude_unset=True), user_id)
    except StaffAssignmentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from exc
    except EventManagementValidationError as exc:
        raise _unprocessable(exc) from exc


@router.delete("/event-staff/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_staff(
    assignment_id: UUID,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    try:
        await delete_staff(assignment_id, user_id)
    except StaffAssignmentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
