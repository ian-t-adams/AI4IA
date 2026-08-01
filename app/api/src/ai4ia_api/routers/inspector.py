"""Consolidated, ownership-scoped Conversation Inspector snapshot."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..conversations.policy import resolve_conversation_policy
from ..library.access import get_accessible_document, list_accessible_documents
from ..library.repository import DocumentNotFoundError
from ..sessions.repository import SessionNotFoundError
from ..usage.models import SessionUsageSummary, UsageSummary
from .documents import DocumentSummary
from .library import UserDocumentSummary

router = APIRouter(prefix="/api/sessions", tags=["inspector"])
logger = logging.getLogger(__name__)


class InspectorModel(BaseModel):
    id: str | None = None
    displayName: str | None = None
    contextWindow: int | None = None
    maxOutputTokens: int | None = None


class InspectorInstructions(BaseModel):
    """The instructions actually driving this conversation, and where to change them.

    ``value`` is the *effective* text: the agent's persona when an agent owns the
    turn, otherwise the session's own prompt. It is returned even when the caller
    cannot edit it here, because the inspector's job is to show what is running --
    withholding it left the panel unable to answer "what is this agent told to do?".
    Both a curated persona and a user agent's prompt are already visible to this
    caller by other means (the persona is shipped in the image; the user agent was
    authored by this user), and ``resolve_conversation_policy`` only ever resolves
    agents from *this caller's* composed catalog.

    ``editable`` means editable *in this panel*; ``agentSource`` says where else to
    go when it is not, since the two answers differ.
    """

    source: str
    editable: bool
    value: str | None = None
    agentName: str | None = None
    agentSource: str | None = None


class InspectorAgent(BaseModel):
    name: str | None = None
    displayName: str | None = None
    description: str | None = None


class InspectorTools(BaseModel):
    inherited: list[str] = Field(default_factory=list)
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    effective: list[str] = Field(default_factory=list)
    voiceEffective: list[str] = Field(default_factory=list)


class InspectorVoice(BaseModel):
    defaultProviderId: str | None = None
    enabledProviderIds: list[str] = Field(default_factory=list)
    applies: str = "next_connection"


class InspectorSnapshot(BaseModel):
    generatedAt: datetime
    sessionId: str
    title: str
    model: InspectorModel
    instructions: InspectorInstructions
    agent: InspectorAgent
    tools: InspectorTools
    attachments: list[DocumentSummary] = Field(default_factory=list)
    libraryDocuments: list[UserDocumentSummary] = Field(default_factory=list)
    librarySelectionMode: str
    sessionUsage: SessionUsageSummary
    monthlyUsage: UsageSummary
    voice: InspectorVoice


@router.get("/{session_id}/inspector", response_model=InspectorSnapshot)
async def get_inspector(
    session_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> InspectorSnapshot:
    try:
        session = await request.app.state.session_repo.get_session(
            user.internal_user_id, session_id
        )
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )

    policy = await resolve_conversation_policy(
        request.app.state, user.internal_user_id, session
    )
    entry = request.app.state.catalog.get(session.model) if session.model else None
    attachments = await request.app.state.session_repo.list_documents(
        user.internal_user_id, session_id
    )

    library_documents: list[UserDocumentSummary] = []
    library = getattr(request.app.state, "document_library", None)
    if library is not None:
        selected_ids = session.libraryDocumentIds
        if selected_ids is None:
            selected_documents = await list_accessible_documents(
                library, user.internal_user_id, email=user.email
            )
        else:
            selected_documents = []
            for document_id in selected_ids:
                try:
                    selected_documents.append(
                        await get_accessible_document(
                            library,
                            user.internal_user_id,
                            document_id,
                            email=user.email,
                        )
                    )
                except DocumentNotFoundError:
                    continue
                except Exception:
                    logger.warning(
                        "inspector document lookup failed; omitting one selection",
                        exc_info=True,
                    )
                    continue
        for document in selected_documents:
            try:
                library_documents.append(UserDocumentSummary.of(document))
            except Exception:
                continue

    usage = request.app.state.usage
    session_usage = await usage.summarize_session(
        user.internal_user_id, session_id
    )
    monthly_usage = await usage.summarize(user.internal_user_id, since_days=30)

    voice_catalog = request.app.state.voice_provider_catalog
    enabled = request.app.state.settings.voice_provider_allowlist_list
    return InspectorSnapshot(
        generatedAt=datetime.now(timezone.utc),
        sessionId=session.id,
        title=session.title,
        model=InspectorModel(
            id=session.model,
            displayName=entry.displayName if entry else None,
            contextWindow=entry.contextWindow if entry else None,
            maxOutputTokens=entry.maxOutputTokens if entry else None,
        ),
        instructions=InspectorInstructions(
            source=policy.instruction_source,
            editable=policy.agent is None,
            value=policy.instructions,
            agentName=policy.agent.name if policy.agent else None,
            agentSource=(
                None
                if policy.agent is None
                else (
                    "curated"
                    if request.app.state.agents.get(policy.agent.name) is not None
                    else "user"
                )
            ),
        ),
        agent=InspectorAgent(
            name=policy.agent.name if policy.agent else None,
            displayName=policy.agent.displayName if policy.agent else None,
            description=policy.agent.description if policy.agent else None,
        ),
        tools=InspectorTools(
            inherited=list(policy.inherited_tools),
            added=list(policy.added_tools),
            removed=list(policy.removed_tools),
            effective=list(policy.effective_tools),
            voiceEffective=list(policy.voice_tools),
        ),
        attachments=[DocumentSummary.of(document) for document in attachments],
        libraryDocuments=library_documents,
        librarySelectionMode=(
            "legacy_all" if session.libraryDocumentIds is None else "explicit"
        ),
        sessionUsage=session_usage,
        monthlyUsage=monthly_usage,
        voice=InspectorVoice(
            defaultProviderId=request.app.state.settings.voice_default_provider_id,
            enabledProviderIds=[
                provider.id
                for provider in voice_catalog.providers
                if provider.id in enabled
            ],
        ),
    )
