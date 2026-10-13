from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

InitiativeStatus = Literal["PLANNED", "ACTIVE", "COMPLETED", "PAUSED", "CANCELLED"]


class InitiativeListItem(BaseModel):
    id: UUID
    name: str
    brand_id: int
    brand_name: str
    channel_id: int
    channel_name: str
    sub_channel_id: int | None = None
    sub_channel_name: str | None = None
    owner_user_id: UUID | None = None
    owner_name: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    budget_amount: float | None = None
    currency: str
    status: InitiativeStatus


class InitiativeListResponse(BaseModel):
    items: list[InitiativeListItem]
    total: int
    limit: int
    offset: int
