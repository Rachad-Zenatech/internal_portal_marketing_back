"""Paid ads (every Paid Search and Paid Social platform): overview, results, campaigns, and the PPC sheet upload."""
from datetime import date, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status

from models.ads_model import (
    AdCampaign,
    AdImportConfirmResult,
    AdImportPreview,
    AdImportUndoResult,
    AdPlatform,
    AdResult,
    AdResultCreate,
    AdResultList,
    AdResultStatus,
    AdResultUpdate,
    AdsCampaignDetail,
    AdsHealth,
    AdsOverview,
    CampaignObjectiveCode,
    InitiativeStatusCode,
)
from services import ads_import_service, ads_overview_service, ads_service
from services.ads_service import AdResultNotFoundError, AdsValidationError
from services.auth_service import require_permission
from services.cost_sheet_import_service import (
    ImportConflictError,
    ImportExpiredError,
    ImportForbiddenError,
    ImportNotFoundError,
)
from services.import_parsing import ImportFileError
from services.spend_service import InitiativeNotFoundError

router = APIRouter()

_VIEW = require_permission("INITIATIVES_VIEW")
_UPDATE = require_permission("INITIATIVES_UPDATE")
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _unprocessable(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


def _result_not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ad result not found")


def _upload_not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")


_DEFAULT_DAYS = 30
_MAX_DAYS = 3 * 366


def _date_range(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    """[from, to], by default the last 30 days up to today."""
    date_to = date_to or date.today()
    date_from = date_from or date_to - timedelta(days=_DEFAULT_DAYS - 1)
    if date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The start date must be on or before the end date.")
    if (date_to - date_from).days >= _MAX_DAYS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Pick a date range of 3 years or less.")
    return date_from, date_to


# --- The Paid Advertising page ---

@router.get("/ads/overview", response_model=AdsOverview, dependencies=[Depends(_VIEW)])
async def ads_overview(
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    brand_id: int | None = None,
    market_id: int | None = None,
    platform: AdPlatform | None = None,
    objective: CampaignObjectiveCode | None = None,
    campaign_status: Annotated[InitiativeStatusCode | None, Query(alias="status")] = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
):
    """Key figures (with the previous period), per platform, brand and month, spend and verified leads over time,
    campaigns needing attention, and every campaign with its figures, in one currency (default: the most used)."""
    date_from, date_to = _date_range(date_from, date_to)
    filters = {"brand_id": brand_id, "market_id": market_id, "platform": platform, "objective": objective,
               "status": campaign_status, "currency": currency}
    return await ads_overview_service.overview(filters, date_from, date_to)


@router.get("/ads/campaigns/{initiative_id}", response_model=AdsCampaignDetail, dependencies=[Depends(_VIEW)])
async def ads_campaign_detail(
    initiative_id: UUID,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
):
    """One campaign for the side panel: figures for the range, trend, funnel, budget pace, CRM results, links."""
    date_from, date_to = _date_range(date_from, date_to)
    try:
        return await ads_overview_service.campaign_detail(initiative_id, date_from, date_to)
    except ads_overview_service.CampaignNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found") from exc


@router.get("/ads/data-health", response_model=AdsHealth, dependencies=[Depends(_VIEW)])
async def ads_data_health(
    brand_id: int | None = None,
    market_id: int | None = None,
    platform: AdPlatform | None = None,
    objective: CampaignObjectiveCode | None = None,
    campaign_status: Annotated[InitiativeStatusCode | None, Query(alias="status")] = None,
):
    """Per platform: where results come from, the latest data date, and campaigns with missing or late data."""
    filters = {"brand_id": brand_id, "market_id": market_id, "platform": platform, "objective": objective,
               "status": campaign_status}
    return await ads_overview_service.health(filters)


# --- Results ---

@router.get("/ads/results", response_model=AdResultList, dependencies=[Depends(_VIEW)])
async def list_ad_results(
    platform: AdPlatform | None = None,
    brand_id: int | None = None,
    market_id: int | None = None,
    initiative_id: UUID | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    result_status: Annotated[AdResultStatus | None, Query(alias="status")] = None,
):
    """Results whose period overlaps [from, to], newest first, with totals per platform and for all."""
    return await ads_service.list_results(platform, brand_id, market_id, initiative_id, date_from, date_to, result_status)


@router.get("/ads/campaigns", response_model=list[AdCampaign], dependencies=[Depends(_VIEW)])
async def list_ad_campaigns(platform: AdPlatform | None = None):
    """Every ad campaign, for pickers."""
    return await ads_service.list_campaigns(platform)


@router.post("/ads/results", response_model=AdResult, status_code=status.HTTP_201_CREATED)
async def create_ad_result(payload: AdResultCreate, user_id: Annotated[UUID, Depends(_UPDATE)]):
    """Add a campaign's (or an ad group's) results for one period. Its spend and results feed costs and performance."""
    try:
        return await ads_service.create_result(payload.model_dump(), user_id)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Initiative not found") from exc
    except AdsValidationError as exc:
        raise _unprocessable(exc) from exc


@router.patch("/ads/results/{result_id}", response_model=AdResult)
async def update_ad_result(result_id: UUID, payload: AdResultUpdate, user_id: Annotated[UUID, Depends(_UPDATE)]):
    """Change some of a result's values; only the fields sent change."""
    try:
        return await ads_service.update_result(result_id, payload.model_dump(exclude_unset=True), user_id)
    except AdResultNotFoundError as exc:
        raise _result_not_found(exc) from exc
    except AdsValidationError as exc:
        raise _unprocessable(exc) from exc


@router.delete("/ads/results/{result_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ad_result(result_id: UUID, user_id: Annotated[UUID, Depends(_UPDATE)]):
    """Delete a result; its cost and synced results go with it (unless Finance reconciled the cost)."""
    try:
        await ads_service.delete_result(result_id, user_id)
    except AdResultNotFoundError as exc:
        raise _result_not_found(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- PPC sheet upload ---

@router.post("/ads/imports", response_model=AdImportPreview, status_code=status.HTTP_201_CREATED)
async def upload_ads_sheet(user_id: Annotated[UUID, Depends(_UPDATE)], file: UploadFile = File(...)):
    """Check a PPC sheet (.xlsx or .csv) and show what would be imported. Nothing is saved until confirmed."""
    data = await file.read(ads_import_service.MAX_FILE_BYTES + 1)
    if len(data) > ads_import_service.MAX_FILE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="The file is larger than 5 MB.")
    try:
        return await ads_import_service.preview_ads_sheet(file.filename or "upload", data, user_id)
    except ImportFileError as exc:
        raise _unprocessable(exc) from exc


@router.post("/ads/imports/{batch_id}/confirm", response_model=AdImportConfirmResult)
async def confirm_ads_import(batch_id: UUID, user_id: Annotated[UUID, Depends(_UPDATE)]):
    """Import the previewed rows, creating any new campaigns."""
    try:
        return await ads_import_service.confirm_import(batch_id, user_id)
    except ImportNotFoundError as exc:
        raise _upload_not_found(exc) from exc
    except ImportForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the person who uploaded this file can confirm it.") from exc
    except ImportConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ImportExpiredError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This preview has expired. Upload the file again.") from exc


@router.post("/ads/imports/{batch_id}/undo", response_model=AdImportUndoResult)
async def undo_ads_import(batch_id: UUID, user_id: Annotated[UUID, Depends(_UPDATE)]):
    """Remove the results the upload added and give the ones it updated their previous values back."""
    try:
        return await ads_import_service.undo_import(batch_id, user_id)
    except ImportNotFoundError as exc:
        raise _upload_not_found(exc) from exc
    except ImportConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/ads/template", dependencies=[Depends(_VIEW)], response_class=Response)
async def download_ads_template():
    """An .xlsx with the same columns as Marketing's "SaaS PPC Campaigns" sheet."""
    return Response(
        content=await ads_import_service.build_template(),
        media_type=_XLSX,
        headers={"Content-Disposition": 'attachment; filename="saas-ppc-campaigns-template.xlsx"'},
    )
