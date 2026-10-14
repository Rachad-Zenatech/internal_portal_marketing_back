"""What the travel management system reads from and reports to this portal. Service tokens only (no people).

See docs/travel-integration.md for the contract.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status

from models.event_management_model import TravelFeedEvent, TravelStatusReport
from services.event_management_service import StaffAssignmentNotFoundError, record_travel_status, travel_feed
from services.service_security_policy import require_application_scope

TRAVEL_SYSTEM = "travel-management"

router = APIRouter()


@router.get(
    "/integrations/travel/events",
    response_model=list[TravelFeedEvent],
    dependencies=[Depends(require_application_scope("events:read", allowed_services=[TRAVEL_SYSTEM]))],
)
async def list_events_for_travel(updated_since: datetime | None = None):
    """Events (current, upcoming, and up to 90 days past) with the staff who need travel and what they need.

    Pass updated_since (ISO time) to get only events whose details or staff changed since then. Someone taken off an
    event no longer appears in its staff list.
    """
    return await travel_feed(updated_since)


@router.post(
    "/integrations/travel/staff-status",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_application_scope("events:travel_status_write", allowed_services=[TRAVEL_SYSTEM]))],
)
async def report_travel_status(payload: TravelStatusReport):
    """The travel system reporting how far booking has got for one person on one event."""
    try:
        await record_travel_status(payload.assignment_id, payload.travel_status)
    except StaffAssignmentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
