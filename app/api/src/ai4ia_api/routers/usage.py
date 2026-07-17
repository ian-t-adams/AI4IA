"""Per-user usage/cost summary endpoint.

Returns an aggregate view of the caller's own metered chat turns (token counts,
request counts by status, and best-effort cost estimates) over a bounded window.
Ownership is enforced by always summarizing ``user.internal_user_id`` — a caller
can never read another user's ledger.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..sessions.repository import SessionNotFoundError
from ..usage.models import SessionUsageSummary, UsageSummary
from ..usage.service import MAX_SUMMARY_DAYS, UsageService

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("", response_model=UsageSummary)
async def get_usage(
    request: Request,
    since_days: int = Query(
        default=30,
        ge=1,
        le=MAX_SUMMARY_DAYS,
        description="Window in days to summarize (1-90).",
    ),
    user: AuthenticatedUser = Depends(get_current_user),
) -> UsageSummary:
    metering: UsageService = request.app.state.usage
    return await metering.summarize(user.internal_user_id, since_days=since_days)


@router.get("/sessions/{session_id}", response_model=SessionUsageSummary)
async def get_session_usage(
    session_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SessionUsageSummary:
    try:
        await request.app.state.session_repo.get_session(
            user.internal_user_id, session_id
        )
    except SessionNotFoundError:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )
    metering: UsageService = request.app.state.usage
    return await metering.summarize_session(user.internal_user_id, session_id)
