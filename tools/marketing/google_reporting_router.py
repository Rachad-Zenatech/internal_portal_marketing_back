"""Permission-protected Google reports using the backend's configured reader."""
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from models.marketing.google_reporting_model import GoogleMarketingReport, GoogleReportingBrand
from services.marketing.google_reporting_accounts import ACCOUNTS, reporting_brands
from services.marketing.google_reporting_service import get_google_report
from services.shared.core.auth_service import require_permission

router = APIRouter(prefix="/marketing/google", dependencies=[Depends(require_permission("INITIATIVES_VIEW"))])


@router.get("/brands", response_model=list[GoogleReportingBrand])
async def get_google_reporting_brands():
    return reporting_brands()


@router.get("/report", response_model=GoogleMarketingReport)
async def google_marketing_report(
    response: Response,
    brand: str = "daas",
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
):
    today = datetime.now(timezone.utc).date()
    end = date_to or today - timedelta(days=4)
    start = date_from or end - timedelta(days=6)
    if brand not in ACCOUNTS:
        raise HTTPException(422, "Choose a configured Google reporting brand.")
    if start > end or (end - start).days > 365 or end >= today:
        raise HTTPException(422, "Choose a past date range of at most 366 days, with start on or before end.")
    response.headers["Cache-Control"] = "no-store"
    try:
        return await get_google_report(brand, start, end)
    except (RuntimeError, TimeoutError):
        raise HTTPException(503, "Google reporting is busy or timed out. Try again shortly.") from None
