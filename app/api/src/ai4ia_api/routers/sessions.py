"""Per-user session + message CRUD. Every operation is ownership-scoped."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..sessions.models import Message, Session
from ..sessions.repository import SessionNotFoundError, SessionRepository

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    title: str | None = None
    model: str | None = None
    systemPrompt: str | None = None


class UpdateSessionRequest(BaseModel):
    title: str | None = None
    model: str | None = None
    systemPrompt: str | None = None


def _repo(request: Request) -> SessionRepository:
    return request.app.state.session_repo


@router.post("", response_model=Session, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Session:
    session = Session(
        userId=user.internal_user_id,
        title=body.title or "New chat",
        model=body.model,
        systemPrompt=body.systemPrompt,
    )
    return await _repo(request).create_session(session)


@router.get("", response_model=list[Session])
async def list_sessions(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[Session]:
    return await _repo(request).list_sessions(user.internal_user_id)


@router.get("/{session_id}", response_model=Session)
async def get_session(
    session_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Session:
    try:
        return await _repo(request).get_session(user.internal_user_id, session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")


@router.patch("/{session_id}", response_model=Session)
async def update_session(
    session_id: str,
    body: UpdateSessionRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Session:
    repo = _repo(request)
    try:
        session = await repo.get_session(user.internal_user_id, session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    data = body.model_dump(exclude_unset=True)
    for field_name, value in data.items():
        setattr(session, field_name, value)
    session.updatedAt = datetime.now(timezone.utc)
    return await repo.update_session(session)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    try:
        await _repo(request).delete_session(user.internal_user_id, session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")


@router.get("/{session_id}/messages", response_model=list[Message])
async def list_messages(
    session_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[Message]:
    try:
        return await _repo(request).list_messages(user.internal_user_id, session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
