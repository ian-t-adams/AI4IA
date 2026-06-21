"""Per-user session + message CRUD. Every operation is ownership-scoped."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..sessions.models import Message, MessageRole, MessageSource, Session
from ..sessions.repository import SessionNotFoundError, SessionRepository

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

# Caps for persisting a Voice Live exchange back into a session. A single live
# conversation is bounded by the relay's max-session clamp, so these only guard
# against an abusive/garbage client payload, not normal use.
MAX_VOICE_TURNS = 200
MAX_VOICE_TURN_CHARS = 8000


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
    # Best-effort purge of any inline-attachment original bytes retained for this
    # session (inline code-interpreter feature). The store no-ops when nothing was
    # retained and never raises, so it can't break the delete.
    store = getattr(request.app.state, "inline_attachment_store", None)
    if store is not None:
        await store.delete_session(user.internal_user_id, session_id)


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


class VoiceTurnInput(BaseModel):
    """A single finalized Voice Live turn the browser is persisting back."""

    role: Literal["user", "assistant"]
    text: str = Field(min_length=1)


class AppendVoiceTurnsRequest(BaseModel):
    turns: list[VoiceTurnInput] = Field(default_factory=list)


def _clean_turn_text(text: str) -> str:
    """Strip control characters (keep newline/tab), trim, and cap length.

    Voice transcripts are model/ASR output relayed verbatim through the browser,
    so they are sanitized the same way typed content is before persisting.
    """
    cleaned = "".join(ch for ch in text if ch in ("\n", "\t") or ord(ch) >= 32)
    return cleaned.strip()[:MAX_VOICE_TURN_CHARS]


@router.post(
    "/{session_id}/voice-turns",
    response_model=list[Message],
    status_code=status.HTTP_201_CREATED,
)
async def append_voice_turns(
    session_id: str,
    body: AppendVoiceTurnsRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[Message]:
    """Persist a finalized Voice Live exchange into a session's transcript.

    Lets text chat and Voice Live share ONE conversation: the live turns land as
    ordinary user/assistant messages (tagged ``source=voice``) so they appear in
    the transcript and feed model context when the user resumes typing. Ownership
    is enforced (404 for a session the caller does not own); empty/whitespace
    turns are dropped; ``createdAt`` is monotonically offset to preserve order.
    """
    repo = _repo(request)
    try:
        session = await repo.get_session(user.internal_user_id, session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if len(body.turns) > MAX_VOICE_TURNS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"too many turns (max {MAX_VOICE_TURNS})",
        )

    base = datetime.now(timezone.utc)
    created: list[Message] = []
    for index, turn in enumerate(body.turns):
        text = _clean_turn_text(turn.text)
        if not text:
            continue
        message = Message(
            sessionId=session_id,
            userId=user.internal_user_id,
            role=MessageRole(turn.role),
            content=text,
            source=MessageSource.voice,
            createdAt=base + timedelta(milliseconds=index),
        )
        created.append(await repo.add_message(user.internal_user_id, message))

    if created:
        session.updatedAt = datetime.now(timezone.utc)
        await repo.update_session(session)

    return created
