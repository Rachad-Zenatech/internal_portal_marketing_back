from fastapi import APIRouter

from models.reference_data.reference_data_model import ReferenceDataResponse
from services.reference_data.reference_data_service import get_reference_data as fetch_reference_data

router = APIRouter()


@router.get("/reference-data", response_model=ReferenceDataResponse)
async def get_reference_data():
    """All marketing lookup lists (brands, channels, goals, ...) for dropdowns."""
    return await fetch_reference_data()
