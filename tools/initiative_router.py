from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from models.initiative_model import (
    InitiativeCreate,
    InitiativeListItem,
    InitiativeListResponse,
    InitiativeStatus,
    OwnerOption,
)
from services.auth_service import require_permission
from services.initiative_service import InitiativeValidationError
from services.initiative_service import create_initiative as insert_initiative
from services.initiative_service import list_initiatives as fetch_initiatives
from services.initiative_service import list_owner_options as fetch_owner_options

router = APIRouter()


@router.get(
    "/initiatives",
    response_model=InitiativeListResponse,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def list_initiatives(
    channel_id: int | None = None,
    sub_channel_id: int | None = None,
    brand_id: int | None = None,
    status: InitiativeStatus | None = None,
    search: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Marketing initiatives across all channels, with optional filters."""
    items, total = await fetch_initiatives(
        channel_id=channel_id,
        sub_channel_id=sub_channel_id,
        brand_id=brand_id,
        status=status,
        search=search,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/initiatives", response_model=InitiativeListItem, status_code=status.HTTP_201_CREATED)
async def create_initiative(
    payload: InitiativeCreate,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_CREATE"))],
):
    """Create an initiative for any channel. Owner defaults to the current user."""
    try:
        return await insert_initiative(payload, user_id)
    except InitiativeValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get(
    "/initiatives/owner-options",
    response_model=list[OwnerOption],
    dependencies=[Depends(require_permission("INITIATIVES_CREATE"))],
)
async def list_owner_options(search: str | None = Query(None, max_length=200)):
    """Active users who can be picked as an initiative's owner."""
    return await fetch_owner_options(search)
