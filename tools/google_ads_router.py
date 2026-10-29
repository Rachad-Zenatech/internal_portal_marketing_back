"""Google Ads: run the campaign and results sync now, and see how it last went."""
from fastapi import APIRouter, Depends, HTTPException, status

from models.google_ads_model import GoogleAdsSyncStatus, GoogleAdsSyncSummary
from services.shared.auth_service import require_permission
from services.ads.google_ads_client import google_ads_configured, google_ads_mode
from services.ads.google_ads_sync_job import (
    GoogleAdsSyncAlreadyRunning,
    GoogleAdsSyncFailed,
    connected_accounts,
    get_google_ads_sync_state,
    google_ads_sync_enabled,
    lookback_days,
    sync_google_ads,
    sync_interval_minutes,
)

router = APIRouter()


@router.post(
    "/integrations/google-ads/sync",
    response_model=GoogleAdsSyncSummary,
    dependencies=[Depends(require_permission("INITIATIVES_UPDATE"))],
)
async def sync_google_ads_now():
    """Read the connected Google Ads accounts now (generated data for mock connections in mock mode) and update their
    campaigns and weekly results."""
    try:
        return await sync_google_ads()
    except GoogleAdsSyncAlreadyRunning as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except GoogleAdsSyncFailed as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get(
    "/integrations/google-ads/sync-status",
    response_model=GoogleAdsSyncStatus,
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def get_google_ads_sync_status():
    """The mode (mock or live), whether the scheduled sync is on and Google Ads is set up, the connected accounts, and
    the last run in this mode."""
    state = await get_google_ads_sync_state() or {}
    return {
        "mode": google_ads_mode(),
        "enabled": google_ads_sync_enabled(),
        "configured": google_ads_configured(),
        "interval_minutes": sync_interval_minutes(),
        "lookback_days": lookback_days(),
        "connected_accounts": await connected_accounts(),
        "last_success_at": state.get("last_success_at"),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_error": state.get("last_error"),
        "last_run_summary": state.get("last_run_summary"),
    }
