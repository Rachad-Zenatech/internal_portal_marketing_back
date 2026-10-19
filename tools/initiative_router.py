from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from models.initiative_model import (
    InitiativeCreate,
    InitiativeListItem,
    InitiativeListResponse,
    InitiativeStatus,
    InitiativeUpdate,
    OwnerOption,
)
from services.auth_service import require_permission
from services.initiative_service import (
    InitiativeNotFoundError,
    InitiativeValidationError,
)
from services.initiative_service import create_initiative as insert_initiative
from services.initiative_service import delete_initiative as remove_initiative
from services.initiative_service import get_initiative as fetch_initiative
from services.initiative_service import list_initiatives as fetch_initiatives
from services.initiative_service import list_owner_options as fetch_owner_options
from services.initiative_service import update_initiative as save_initiative_changes

router = APIRouter()


@router.get(
    "/initiatives",
    response_model=InitiativeListResponse,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def list_initiatives(
    channel_id: int | None = None,
    channel_ids: list[int] | None = Query(None, description="Several channels at once, e.g. ?channel_ids=7&channel_ids=8"),
    sub_channel_id: int | None = None,
    brand_id: int | None = None,
    market_id: int | None = None,
    status: InitiativeStatus | None = None,
    search: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Marketing initiatives across all channels, with optional filters."""
    items, total = await fetch_initiatives(
        channel_id=channel_id,
        channel_ids=channel_ids,
        sub_channel_id=sub_channel_id,
        brand_id=brand_id,
        market_id=market_id,
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
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def list_owner_options(search: str | None = Query(None, max_length=200)):
    """Active users who can be picked as an initiative's owner."""
    return await fetch_owner_options(search)


# Declared after /initiatives/owner-options so that path isn't read as an id
@router.get(
    "/initiatives/{initiative_id}",
    response_model=InitiativeListItem,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def get_initiative(initiative_id: UUID):
    try:
        return await fetch_initiative(initiative_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Initiative not found") from exc


@router.patch("/initiatives/{initiative_id}", response_model=InitiativeListItem)
async def update_initiative(
    initiative_id: UUID,
    payload: InitiativeUpdate,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Change any of an initiative's fields; only the fields sent are updated."""
    try:
        return await save_initiative_changes(initiative_id, payload.model_dump(exclude_unset=True), user_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Initiative not found") from exc
    except InitiativeValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/initiatives/{initiative_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_initiative(
    initiative_id: UUID,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_DELETE"))],
):
    """Delete an initiative in any status, events included."""
    try:
        await remove_initiative(initiative_id, user_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Initiative not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
