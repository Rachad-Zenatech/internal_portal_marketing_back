from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

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
    funnel_stage_id: int | None = None
    funnel_stage_name: str | None = None
    goal_id: int | None = None
    goal_name: str | None = None
    primary_kpi_id: int | None = None
    primary_kpi_name: str | None = None
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


class InitiativeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    brand_id: int
    channel_id: int
    sub_channel_id: int | None = None
    funnel_stage_id: int | None = None
    goal_id: int | None = None
    primary_kpi_id: int | None = None
    # Defaults to the user creating the initiative
    owner_user_id: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    budget_amount: float | None = Field(default=None, ge=0, le=999_999_999_999)
    currency: str = Field(default="USD", pattern=r"^[A-Za-z]{3}$")
    status: InitiativeStatus = "PLANNED"

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name is required")
        return value

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def check_dates_and_goal(self) -> "InitiativeCreate":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("End date can't be before the start date")
        if self.goal_id is not None and self.funnel_stage_id is None:
            raise ValueError("Choose a funnel stage before choosing a goal")
        return self


class InitiativeUpdate(BaseModel):
    """Partial update: only the fields sent are changed. Send null to clear an optional field."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    brand_id: int | None = None
    channel_id: int | None = None
    sub_channel_id: int | None = None
    funnel_stage_id: int | None = None
    goal_id: int | None = None
    primary_kpi_id: int | None = None
    owner_user_id: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    budget_amount: float | None = Field(default=None, ge=0, le=999_999_999_999)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    status: InitiativeStatus | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Name is required")
        return value

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @model_validator(mode="after")
    def check_required_fields(self) -> "InitiativeUpdate":
        # These can be changed but not cleared
        for field in ("name", "brand_id", "channel_id", "currency", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field.replace('_id', '').replace('_', ' ').capitalize()} can't be empty")
        return self


class OwnerOption(BaseModel):
    id: UUID
    full_name: str | None = None
    email: str
