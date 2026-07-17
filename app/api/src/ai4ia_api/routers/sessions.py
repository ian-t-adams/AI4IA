"""Per-user session + message CRUD. Every operation is ownership-scoped."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..agents.mcp_servers import namespaced_tool_name
from ..library.access import get_accessible_document, list_accessible_documents
from ..library.repository import DocumentNotFoundError
from ..sessions.models import (
    MAX_LIBRARY_DOCUMENTS_PER_SESSION,
    Message,
    MessageRole,
    MessageSource,
    Session,
    ToolOverrides,
)
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
    agentName: str | None = None
    toolOverrides: ToolOverrides = Field(default_factory=ToolOverrides)
    libraryDocumentIds: list[str] | None = Field(
        default=None, max_length=MAX_LIBRARY_DOCUMENTS_PER_SESSION
    )


class UpdateSessionRequest(BaseModel):
    title: str | None = None
    model: str | None = None
    systemPrompt: str | None = None
    agentName: str | None = None
    toolOverrides: ToolOverrides | None = None
    libraryDocumentIds: list[str] | None = Field(
        default=None, max_length=MAX_LIBRARY_DOCUMENTS_PER_SESSION
    )


def _repo(request: Request) -> SessionRepository:
    return request.app.state.session_repo


async def _conversation_addable_tools(request: Request, user_id: str) -> set[str]:
    allowed = set(request.app.state.agent_service.attachable_tools)
    service = getattr(request.app.state, "mcp_service", None)
    if service is not None:
        try:
            allowed.update(
                namespaced_tool_name(server.name, tool.name)
                for server in await service.list_for(user_id)
                for tool in server.discoveredTools
            )
        except Exception:
            pass
    official = getattr(request.app.state, "official_mcp_service", None)
    if official is not None:
        try:
            allowed.update(
                namespaced_tool_name(server.name, tool.name)
                for server in await official.list_all()
                for tool in server.discoveredTools
            )
        except Exception:
            pass
    return allowed


async def _validate_policy_fields(
    request: Request,
    user: AuthenticatedUser,
    *,
    agent_name: str | None,
    overrides: ToolOverrides,
    library_document_ids: list[str] | None,
    validate_agent: bool = True,
    validate_tools: bool = True,
    validate_documents: bool = True,
) -> tuple[str | None, ToolOverrides, list[str] | None]:
    selected = (agent_name or "").strip() or None
    if selected and validate_agent:
        catalog = await request.app.state.agent_service.catalog_for(
            user.internal_user_id, request.app.state.agents
        )
        agent = catalog.get(selected)
        if agent is None or not agent.enabled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unknown or disabled agent: {selected}",
            )
        selected = agent.name

    def clean_tools(values: list[str]) -> list[str]:
        return list(dict.fromkeys((value or "").strip() for value in values if value.strip()))

    added = clean_tools(overrides.added)
    removed = clean_tools(overrides.removed)
    if len(added) > 8 or len(removed) > 16:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Too many conversation tool overrides.",
        )
    unavailable = (
        set(added)
        - await _conversation_addable_tools(request, user.internal_user_id)
        if validate_tools
        else set()
    )
    if unavailable:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Tools are not available for conversation overrides: {', '.join(sorted(unavailable))}",
        )

    document_ids = (
        None
        if library_document_ids is None
        else list(
            dict.fromkeys(
                (value or "").strip()
                for value in library_document_ids
                if value.strip()
            )
        )
    )
    if document_ids and validate_documents:
        library = getattr(request.app.state, "document_library", None)
        if library is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="The document library is not enabled.",
            )
        for document_id in document_ids:
            try:
                await get_accessible_document(
                    library,
                    user.internal_user_id,
                    document_id,
                    email=user.email,
                )
            except DocumentNotFoundError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Library document is unavailable: {document_id}",
                ) from exc
    return selected, ToolOverrides(added=added, removed=removed), document_ids


@router.post("", response_model=Session, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Session:
    agent_name, overrides, document_ids = await _validate_policy_fields(
        request,
        user,
        agent_name=body.agentName,
        overrides=body.toolOverrides,
        library_document_ids=body.libraryDocumentIds,
    )
    session = Session(
        userId=user.internal_user_id,
        title=body.title or "New chat",
        model=body.model,
        systemPrompt=body.systemPrompt,
        agentName=agent_name,
        toolOverrides=overrides,
        libraryDocumentIds=document_ids,
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
    next_agent = data.get("agentName", session.agentName)
    next_overrides = data.get("toolOverrides", session.toolOverrides)
    next_document_ids = data.get("libraryDocumentIds", session.libraryDocumentIds)
    next_agent, next_overrides, next_document_ids = await _validate_policy_fields(
        request,
        user,
        agent_name=next_agent,
        overrides=next_overrides,
        library_document_ids=next_document_ids,
        validate_agent="agentName" in data,
        validate_tools="toolOverrides" in data,
        validate_documents="libraryDocumentIds" in data,
    )
    data["agentName"] = next_agent
    data["toolOverrides"] = next_overrides
    data["libraryDocumentIds"] = next_document_ids
    for field_name, value in data.items():
        setattr(session, field_name, value)
    session.updatedAt = datetime.now(timezone.utc)
    return await repo.update_session(session)


@router.post("/{session_id}/library-documents/{document_id}", response_model=Session)
async def associate_library_document(
    session_id: str,
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Session:
    repo = _repo(request)
    try:
        session = await repo.get_session(user.internal_user_id, session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    library = getattr(request.app.state, "document_library", None)
    if library is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The document library is not enabled.",
        )
    try:
        await get_accessible_document(
            library,
            user.internal_user_id,
            document_id,
            email=user.email,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        ) from exc
    if session.libraryDocumentIds is None:
        # Legacy all-access mode already includes every owned document, including
        # the newly associated one. Preserve the sentinel instead of narrowing it.
        return session
    selected = list(session.libraryDocumentIds)
    if document_id not in selected:
        selected.append(document_id)
    session.libraryDocumentIds = selected
    session.updatedAt = datetime.now(timezone.utc)
    return await repo.update_session(session)


@router.delete("/{session_id}/library-documents/{document_id}", response_model=Session)
async def disassociate_library_document(
    session_id: str,
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Session:
    repo = _repo(request)
    try:
        session = await repo.get_session(user.internal_user_id, session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if session.libraryDocumentIds is None:
        library = getattr(request.app.state, "document_library", None)
        docs = (
            await list_accessible_documents(
                library, user.internal_user_id, email=user.email
            )
            if library is not None
            else []
        )
        session.libraryDocumentIds = [
            document.id for document in docs if document.id != document_id
        ]
    else:
        session.libraryDocumentIds = [
            value for value in session.libraryDocumentIds if value != document_id
        ]
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
    createdAt: datetime | None = None


class AppendVoiceTurnsRequest(BaseModel):
    conversationId: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
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
    New clients provide ``conversationId`` so retries deterministically upsert the
    same message ids instead of duplicating an exchange after a lost response.
    """
    repo = _repo(request)
    try:
        await repo.get_session(user.internal_user_id, session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if len(body.turns) > MAX_VOICE_TURNS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"too many turns (max {MAX_VOICE_TURNS})",
        )

    existing_by_id: dict[str, Message] = {}
    if body.conversationId:
        existing_by_id = {
            message.id: message
            for message in await repo.list_messages(user.internal_user_id, session_id)
        }

    base = datetime.now(timezone.utc)
    created: list[Message] = []
    for index, turn in enumerate(body.turns):
        text = _clean_turn_text(turn.text)
        if not text:
            continue
        message_id: str | None = None
        if body.conversationId:
            fingerprint = "\0".join(
                (
                    user.internal_user_id,
                    session_id,
                    body.conversationId,
                    str(index),
                    turn.role,
                    text,
                )
            )
            message_id = f"voice-{sha256(fingerprint.encode('utf-8')).hexdigest()}"
            if existing := existing_by_id.get(message_id):
                created.append(existing)
                continue
        created_at = turn.createdAt or base + timedelta(milliseconds=index)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        message = Message(
            sessionId=session_id,
            userId=user.internal_user_id,
            role=MessageRole(turn.role),
            content=text,
            source=MessageSource.voice,
            createdAt=created_at,
        )
        if message_id:
            message.id = message_id
        if message_id:
            created.append(await repo.upsert_message(user.internal_user_id, message))
        else:
            created.append(await repo.add_message(user.internal_user_id, message))

    if created:
        await repo.touch_session(user.internal_user_id, session_id)

    return created
