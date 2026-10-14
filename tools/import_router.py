from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status

from models.import_model import CostSheetPreview, ImportBatchSummary, ImportConfirmResult, ImportUndoResult
from services.auth_service import require_permission
from services.cost_sheet_import_service import (
    MAX_FILE_BYTES,
    ImportConflictError,
    ImportExpiredError,
    ImportForbiddenError,
    ImportNotFoundError,
)
from services.cost_sheet_import_service import build_template as build_cost_sheet_template
from services.cost_sheet_import_service import confirm_import as confirm_upload
from services.cost_sheet_import_service import list_imports as fetch_imports
from services.cost_sheet_import_service import preview_cost_sheet as check_cost_sheet
from services.cost_sheet_import_service import undo_import as undo_upload
from services.import_parsing import ImportFileError
from services.spend_service import InitiativeNotFoundError

router = APIRouter()

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _initiative_not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Initiative not found")


@router.post("/initiatives/{initiative_id}/imports/cost-sheet", response_model=CostSheetPreview, status_code=status.HTTP_201_CREATED)
async def upload_cost_sheet(
    initiative_id: UUID,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
    file: UploadFile = File(...),
):
    """Check an event's cost sheet (.xlsx or .csv) and show what would be imported. Nothing is saved until confirmed."""
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="The file is larger than 5 MB.")
    try:
        return await check_cost_sheet(initiative_id, file.filename or "upload", data, user_id)
    except InitiativeNotFoundError as exc:
        raise _initiative_not_found(exc) from exc
    except ImportFileError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get(
    "/imports/cost-sheet/template",
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
    response_class=Response,
)
async def download_cost_sheet_template():
    """An .xlsx to fill in: the columns in order, an example booth, and dropdowns for the categories."""
    return Response(
        content=await build_cost_sheet_template(),
        media_type=_XLSX,
        headers={"Content-Disposition": 'attachment; filename="event-cost-sheet-template.xlsx"'},
    )


@router.post("/imports/{batch_id}/confirm", response_model=ImportConfirmResult)
async def confirm_cost_sheet(
    batch_id: UUID,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Import the previewed rows as cost lines."""
    try:
        return await confirm_upload(batch_id, user_id)
    except ImportNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found") from exc
    except ImportForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the person who uploaded this file can confirm it.") from exc
    except ImportConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ImportExpiredError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This preview has expired. Upload the file again.") from exc


@router.post("/imports/{batch_id}/undo", response_model=ImportUndoResult)
async def undo_cost_sheet(
    batch_id: UUID,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Remove every cost line the upload created, including any edited since."""
    try:
        return await undo_upload(batch_id, user_id)
    except ImportNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found") from exc
    except ImportConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get(
    "/initiatives/{initiative_id}/imports",
    response_model=list[ImportBatchSummary],
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def list_cost_sheet_imports(initiative_id: UUID):
    """This initiative's recent cost sheet uploads, newest first."""
    try:
        return await fetch_imports(initiative_id)
    except InitiativeNotFoundError as exc:
        raise _initiative_not_found(exc) from exc
