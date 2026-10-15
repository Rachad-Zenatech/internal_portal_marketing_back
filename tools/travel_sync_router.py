"""People-facing controls for the travel cost sync: run it now, and see how it last went.

Kept apart from tools/travel_integration_router.py, which serves the travel system itself with service tokens and
is mounted without user sign-in. Mount this one with the signed-in user dependency like the other user routers.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from models.travel_integration_model import TravelSyncStatus, TravelSyncSummary
from services.auth_service import require_permission
from services.travel_cost_sync_job import (
    TravelSyncAlreadyRunning,
    get_travel_sync_state,
    sync_travel_costs,
    travel_cost_sync_enabled,
)
from services.travel_integration_service import TravelUnavailable, travel_backend_url, travel_status

router = APIRouter()


@router.post(
    "/integrations/travel/sync-costs",
    response_model=TravelSyncSummary,
    dependencies=[Depends(require_permission("INITIATIVES_UPDATE"))],
)
async def sync_travel_costs_now(
    event_code: Annotated[str | None, Query(max_length=20, description="Only this event, e.g. EVT-2026-0003")] = None,
):
    """Pull approved travel costs from the travel system now, for every event or just one."""
    try:
        return await sync_travel_costs(event_code)
    except TravelSyncAlreadyRunning as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except TravelUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get(
    "/integrations/travel/sync-status",
    response_model=TravelSyncStatus,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def get_travel_sync_status():
    """Whether the travel cost sync is on and set up, and how its last run went."""
    state = await get_travel_sync_state() or {}
    return {
        "enabled": travel_cost_sync_enabled(),
        "configured": travel_backend_url() is not None,
        "last_success_at": state.get("last_success_at"),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_error": state.get("last_error"),
        "last_run_summary": state.get("last_run_summary"),
        "circuit_state": travel_status()["state"],
    }
