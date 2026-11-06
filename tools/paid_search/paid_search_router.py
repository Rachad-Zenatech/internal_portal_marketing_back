"""The Paid Search board: load the saved entries, and save the whole board."""
from uuid import UUID

from fastapi import APIRouter, Depends

from models.paid_search.paid_search_model import PaidSearchBoardSave, PaidSearchBoardSaved, PaidSearchEntry
from services.paid_search.paid_search_service import list_paid_search_entries, save_paid_search_board
from services.shared.auth_service import require_permission

router = APIRouter()


@router.get(
    "/paid-search/entries",
    response_model=list[PaidSearchEntry],
    dependencies=[Depends(require_permission("INITIATIVES_VIEW"))],
)
async def get_paid_search_entries():
    """Every entry on the Paid Search board, in board order."""
    return await list_paid_search_entries()


@router.put("/paid-search/entries", response_model=PaidSearchBoardSaved)
async def put_paid_search_entries(body: PaidSearchBoardSave, user_id: UUID = Depends(require_permission("INITIATIVES_UPDATE"))):
    """Save the whole board, in order: rows not in it are removed. The last save wins."""
    return {"saved": await save_paid_search_board(body.entries, user_id)}
