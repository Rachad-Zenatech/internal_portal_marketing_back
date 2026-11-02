"""HubSpot campaigns: list the synced campaigns, start the sync, and see how it last went."""
from fastapi import APIRouter, Depends, HTTPException, status

from models.hubspot.hubspot_model import HubSpotCampaign, HubSpotSyncStarted, HubSpotSyncStatus
from services.shared.auth_service import require_permission
from services.hubspot.hubspot_campaign_sync_job import (
    HubSpotSyncAlreadyRunning,
    get_hubspot_sync_state,
    hubspot_sync_enabled,
    hubspot_sync_running,
    list_hubspot_campaigns,
    start_hubspot_sync,
    sync_interval_minutes,
)
from services.hubspot.hubspot_client import hubspot_configured

router = APIRouter()


@router.get(
    "/hubspot/campaigns",
    response_model=list[HubSpotCampaign],
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def get_hubspot_campaigns():
    """Every HubSpot campaign from the last sync, with its report figures, assets and linked initiative."""
    return await list_hubspot_campaigns()


@router.post(
    "/integrations/hubspot/sync",
    response_model=HubSpotSyncStarted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission("INITIATIVES_UPDATE"))],
)
async def sync_hubspot_now():
    """Start reading every HubSpot campaign now. Returns at once; the status shows when it's done."""
    if not hubspot_configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="HubSpot isn't set up. Add HUBSPOT_API_KEY to the settings.")
    try:
        start_hubspot_sync()
    except HubSpotSyncAlreadyRunning as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"started": True}


@router.get(
    "/integrations/hubspot/sync-status",
    response_model=HubSpotSyncStatus,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def get_hubspot_sync_status():
    """Whether HubSpot is set up, whether the scheduled sync is on, whether a sync is running, and the last run."""
    state = await get_hubspot_sync_state() or {}
    return {
        "configured": hubspot_configured(),
        "enabled": hubspot_sync_enabled(),
        "interval_minutes": sync_interval_minutes(),
        "running": hubspot_sync_running(),
        "last_success_at": state.get("last_success_at"),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_error": state.get("last_error"),
        "last_run_summary": state.get("last_run_summary"),
    }
