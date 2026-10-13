from fastapi import APIRouter, Depends, Query

from models.initiative_model import InitiativeListResponse, InitiativeStatus
from services.auth_service import require_permission
from services.initiative_service import list_initiatives as fetch_initiatives

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
