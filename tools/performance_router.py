from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from models.performance_model import (
    Decision,
    DecisionInput,
    Evaluation,
    EvaluationInput,
    OutcomeList,
    OutcomePeriodInput,
    Performance,
)
from services.shared.auth_service import require_permission
from services.performance.performance_service import InitiativeNotFoundError, PerformanceValidationError
from services.performance.performance_service import get_evaluation as fetch_evaluation
from services.performance.performance_service import get_performance as fetch_performance
from services.performance.performance_service import list_decisions as fetch_decisions
from services.performance.performance_service import list_outcomes as fetch_outcomes
from services.performance.performance_service import record_decision as insert_decision
from services.performance.performance_service import save_evaluation as store_evaluation
from services.performance.performance_service import save_outcomes as store_outcomes

router = APIRouter()

_VIEW = [Depends(require_permission("INITIATIVES_VIEW"))]


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Initiative not found")


def _unprocessable(exc: PerformanceValidationError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


@router.get("/initiatives/{initiative_id}/performance", response_model=Performance, dependencies=_VIEW)
async def get_performance(initiative_id: UUID):
    """Cost per outcome, ROI, the suggested rating and the latest decision. Calculated on request, never stored.

    Cost is the expected total (estimates included) in the initiative's currency; other currencies aren't converted.
    Synced results win over typed-in ones for the same month; an event's demos come from its demo log.
    """
    try:
        return await fetch_performance(initiative_id)
    except InitiativeNotFoundError as exc:
        raise _not_found(exc) from exc


@router.get("/initiatives/{initiative_id}/outcomes", response_model=OutcomeList, dependencies=_VIEW)
async def list_outcomes(initiative_id: UUID):
    """Every result by month and source (typed in, synced, or counted from the demo log)."""
    try:
        return await fetch_outcomes(initiative_id)
    except InitiativeNotFoundError as exc:
        raise _not_found(exc) from exc


@router.put("/initiatives/{initiative_id}/outcomes/{period_start}", response_model=OutcomeList)
async def save_outcomes(
    initiative_id: UUID,
    period_start: date,
    payload: OutcomePeriodInput,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Save the typed-in results for one month (period_start is the first of the month). null removes a value."""
    try:
        return await store_outcomes(initiative_id, period_start, [v.model_dump() for v in payload.values], user_id)
    except InitiativeNotFoundError as exc:
        raise _not_found(exc) from exc
    except PerformanceValidationError as exc:
        raise _unprocessable(exc) from exc


@router.get("/initiatives/{initiative_id}/evaluation", response_model=Evaluation, dependencies=_VIEW)
async def get_evaluation(initiative_id: UUID):
    """The maturity window (how long to wait before judging): the sub-channel's default until someone sets it."""
    try:
        return await fetch_evaluation(initiative_id)
    except InitiativeNotFoundError as exc:
        raise _not_found(exc) from exc


@router.put("/initiatives/{initiative_id}/evaluation", response_model=Evaluation)
async def save_evaluation(
    initiative_id: UUID,
    payload: EvaluationInput,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    try:
        return await store_evaluation(initiative_id, payload.maturity_days, user_id)
    except InitiativeNotFoundError as exc:
        raise _not_found(exc) from exc


@router.get("/initiatives/{initiative_id}/decisions", response_model=list[Decision], dependencies=_VIEW)
async def list_decisions(initiative_id: UUID):
    """Scale-or-cut decisions, newest first."""
    try:
        return await fetch_decisions(initiative_id)
    except InitiativeNotFoundError as exc:
        raise _not_found(exc) from exc


@router.post("/initiatives/{initiative_id}/decisions", response_model=Decision, status_code=status.HTTP_201_CREATED)
async def record_decision(
    initiative_id: UUID,
    payload: DecisionInput,
    user_id: Annotated[UUID, Depends(require_permission("INITIATIVES_UPDATE"))],
):
    """Record Marketing's decision; the rating the system suggested at the time is kept beside it."""
    try:
        return await insert_decision(initiative_id, payload.scale_or_cut_id, payload.note, user_id)
    except InitiativeNotFoundError as exc:
        raise _not_found(exc) from exc
    except PerformanceValidationError as exc:
        raise _unprocessable(exc) from exc
