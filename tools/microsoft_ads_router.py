"""Microsoft Ads: run the campaign and results sync now, and see how it last went."""
from fastapi import APIRouter, Depends, HTTPException, status

from models.ads.microsoft_ads_model import MicrosoftAdsSyncStatus, MicrosoftAdsSyncSummary
from services.shared.auth_service import require_permission
from services.ads.microsoft_ads_client import microsoft_ads_configured, microsoft_ads_mode
from services.ads.microsoft_ads_sync_job import (
    MicrosoftAdsSyncAlreadyRunning,
    MicrosoftAdsSyncFailed,
    connected_accounts,
    get_microsoft_ads_sync_state,
    lookback_days,
    microsoft_ads_sync_enabled,
    sync_interval_minutes,
    sync_microsoft_ads,
)

router = APIRouter()


@router.post(
    "/integrations/microsoft-ads/sync",
    response_model=MicrosoftAdsSyncSummary,
    dependencies=[Depends(require_permission("INITIATIVES_UPDATE"))],
)
async def sync_microsoft_ads_now():
    """Read the connected Microsoft Ads accounts now (generated data for mock connections in mock mode) and update
    their campaigns and weekly results."""
    try:
        return await sync_microsoft_ads()
    except MicrosoftAdsSyncAlreadyRunning as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except MicrosoftAdsSyncFailed as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get(
    "/integrations/microsoft-ads/sync-status",
    response_model=MicrosoftAdsSyncStatus,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def get_microsoft_ads_sync_status():
    """The mode (mock or live), whether the scheduled sync is on and Microsoft Ads is set up, the connected accounts,
    and the last run in this mode."""
    state = await get_microsoft_ads_sync_state() or {}
    return {
        "mode": microsoft_ads_mode(),
        "enabled": microsoft_ads_sync_enabled(),
        "configured": microsoft_ads_configured(),
        "interval_minutes": sync_interval_minutes(),
        "lookback_days": lookback_days(),
        "connected_accounts": await connected_accounts(),
        "last_success_at": state.get("last_success_at"),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_error": state.get("last_error"),
        "last_run_summary": state.get("last_run_summary"),
    }
