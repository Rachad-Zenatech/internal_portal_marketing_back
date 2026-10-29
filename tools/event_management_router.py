from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from models.events.event_management_model import (
    EventOverviewItem,
    EventTraveller,
    StaffAssignment,
    StaffAssignmentCreate,
    StaffAssignmentUpdate,
)
from models.events.event_performance_model import EventPerformanceResponse
from models.shared.initiative_model import InitiativeStatus
from services.shared.auth_service import require_permission
from services.events.event_management_service import (
    EventManagementValidationError,
    InitiativeNotFoundError,
    StaffAssignmentNotFoundError,
)
from services.events.event_management_service import add_staff as insert_staff
from services.events.event_management_service import list_event_overview as fetch_overview
from services.events.event_management_service import list_staff as fetch_staff
from services.events.event_management_service import list_travellers as fetch_travellers
from services.events.event_management_service import remove_staff as delete_staff
from services.events.event_management_service import update_staff as save_staff_changes
from services.events.event_performance_service import list_event_performance as fetch_performance

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
    """Every event with what still needs doing — staff, costs, demos, results.

    On now first, then upcoming (soonest first), then past (most recent first).
    """
    return await fetch_overview(search, timing)


@router.get(
    "/events/performance",
    response_model=EventPerformanceResponse,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def list_event_performance(
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    brand_id: int | None = None,
    market_id: int | None = None,
    event_type_id: int | None = None,
    owner_user_id: UUID | None = None,
    event_status: InitiativeStatus | None = Query(None, alias="status"),
):
    """Events Overview: approved budget, actual spend, budget variance, prior year spend, leads and cost per lead
    per currency (with the previous period), monthly budget vs actual vs prior year, the funnel, the cost
    breakdown, and each event with its prior year cost, results and ROI.

    Non-draft events in the Events channel whose dates overlap [from, to]; undated events are left out.
    market_id is the Region filter. Newest start date first.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The start of the period can't be after its end.")
    return await fetch_performance(
        date_from=date_from, date_to=date_to, brand_id=brand_id, market_id=market_id,
        event_type_id=event_type_id, owner_user_id=owner_user_id, status=event_status,
    )


@router.get(
    "/initiatives/{initiative_id}/staff",
    response_model=list[StaffAssignment],
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def list_staff(initiative_id: UUID):
    """The team going to an event: who, their role and notes."""
    try:
        return await fetch_staff(initiative_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found") from exc


@router.get(
    "/initiatives/{initiative_id}/travellers",
    response_model=list[EventTraveller],
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def list_travellers(initiative_id: UUID):
    """The people travelling to an event, from its active Navan bookings (kept by the Navan sync), by name."""
    try:
        return await fetch_travellers(initiative_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found") from exc


@router.post("/initiatives/{initiative_id}/staff", response_model=StaffAssignment, status_code=status.HTTP_201_CREATED)
async def add_staff(
    initiative_id: UUID,
    payload: StaffAssignmentCreate,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Put someone on an event's team."""
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
