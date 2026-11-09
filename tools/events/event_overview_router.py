"""Events Overview: the Navan events with the HubSpot results matched to them, one event in full, and the HubSpot link."""
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from models.events.event_overview_model import (
    EventOverviewDetail,
    EventsOverviewResponse,
    HubSpotLinkUpdate,
    HubSpotOptions,
    NavanTripLinkUpdate,
    NavanTripOption,
)
from models.shared.initiative_model import InitiativeStatus
from services.events.event_management_service import InitiativeNotFoundError
from services.events.event_overview_service import (
    get_event_overview,
    list_events_overview,
    list_hubspot_options,
    list_navan_trip_options,
    set_hubspot_link,
    set_navan_trip_link,
)
from services.shared.core.auth_service import require_permission

router = APIRouter()


@router.get(
    "/events/navan-events",
    response_model=EventsOverviewResponse,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def navan_events_overview(
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    brand_id: int | None = None,
    market_id: int | None = None,
    event_type_id: int | None = None,
    owner_user_id: UUID | None = None,
    event_status: InitiativeStatus | None = Query(None, alias="status"),
):
    """The HubSpot events whose dates overlap the period, with the Navan trips joined to them: budget, cost, travel,
    the HubSpot results, and totals, months and the cost split for the dashboard. Navan trips that match no HubSpot
    event aren't listed."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The start of the period can't be after its end.")
    return await list_events_overview(
        date_from=date_from, date_to=date_to, brand_id=brand_id, market_id=market_id, event_type_id=event_type_id,
        owner_user_id=owner_user_id, status=event_status,
    )


@router.get(
    "/events/navan-events/hubspot-options",
    response_model=HubSpotOptions,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def hubspot_options():
    """Every HubSpot campaign and marketing event, for picking an event's match."""
    return await list_hubspot_options()


@router.get(
    "/events/navan-events/navan-options",
    response_model=list[NavanTripOption],
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def navan_trip_options():
    """The Navan trips that aren't joined to a HubSpot event, for joining one to an event."""
    return await list_navan_trip_options()


@router.get(
    "/events/navan-events/{initiative_id}",
    response_model=EventOverviewDetail,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def navan_event_detail(initiative_id: UUID):
    """One Navan event in full: money, travellers, HubSpot results, who attended and the campaign's assets."""
    try:
        return await get_event_overview(initiative_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found") from exc


@router.put(
    "/events/navan-events/{initiative_id}/hubspot-link",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def link_event_to_hubspot(
    initiative_id: UUID,
    body: HubSpotLinkUpdate,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Pick the HubSpot campaign and/or marketing event of an event, mark it as having none, or send neither to go
    back to the name match."""
    try:
        await set_hubspot_link(initiative_id, body.campaign_id, body.marketing_event_id, body.none, user_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/events/navan-events/{initiative_id}/navan-link",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def link_navan_trip(
    initiative_id: UUID,
    body: NavanTripLinkUpdate,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Join a Navan trip to this HubSpot event (its travel then counts for the event), or take it off again."""
    try:
        await set_navan_trip_link(initiative_id, body.trip_id, body.link, user_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
