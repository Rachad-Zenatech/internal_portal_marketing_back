"""Navan travel costs: run the sync now, see how it last went, and list the Navan events it found."""
from fastapi import APIRouter, Depends, HTTPException, status

from models.navan.navan_model import NavanSyncStatus, NavanSyncSummary
from services.shared.auth_service import require_permission
from services.navan.navan_cost_sync_job import (
    NavanSyncAlreadyRunning,
    get_navan_sync_state,
    navan_cost_sync_enabled,
    sync_navan_costs,
)
from services.navan.navan_service import DEMO_MODE, NavanUnavailable, navan_configured, navan_mode

router = APIRouter()


@router.post(
    "/integrations/navan/sync-costs",
    response_model=NavanSyncSummary,
    dependencies=[Depends(require_permission("INITIATIVES_UPDATE"))],
)
async def sync_navan_costs_now():
    """Read Navan bookings now (generated ones for the demo events in demo mode) and update each linked event's
    travel cost."""
    try:
        return await sync_navan_costs()
    except NavanSyncAlreadyRunning as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except NavanUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get(
    "/integrations/navan/sync-status",
    response_model=NavanSyncStatus,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def get_navan_sync_status():
    """Whether the Navan sync is on and set up, its mode (live or demo data), and its last run in that mode,
    including the Navan events it found."""
    state = await get_navan_sync_state() or {}
    mode = navan_mode()
    return {
        "mode": mode,
        "enabled": navan_cost_sync_enabled(),
        # Demo mode needs no Navan credential
        "configured": mode == DEMO_MODE or navan_configured(),
        "last_success_at": state.get("last_success_at"),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_error": state.get("last_error"),
        "last_run_summary": state.get("last_run_summary"),
    }
