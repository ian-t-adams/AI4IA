"""Per-user document library API (Phase 11A storage spine).

Feature-flagged and default-OFF: when ``document_understanding_enabled`` is
false the library repository is never constructed (``app.state.document_library``
is ``None``) and every route here refuses with 404, so the app's default
behavior is unchanged. When enabled, this exposes the user's cross-session
document manifests (list/get/delete) and the analyzer registry (built-ins +
per-user custom CRUD). Ownership is enforced on every operation.

Content Understanding ingest (upload → blob → crack → index) lands in 11B; 11A
ships only the storage spine so the data model and governance are settled first.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..entitlements.service import EntitlementService
from ..library.access import can_access, require_owner
from ..library.models import (
    Analyzer,
    AnalyzerKind,
    DocumentStatus,
    Modality,
    UserDocument,
    Visibility,
)
from ..library.repository import (
    AnalyzerConflictError,
    AnalyzerNotFoundError,
    DocumentLibraryRepository,
    DocumentNotFoundError,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/library", tags=["library"])


class UserDocumentSummary(BaseModel):
    """Library document metadata returned to the client. Excludes reserved
    internal fields (e.g. ``acl``) and never carries the document body."""

    id: str
    filename: str
    contentType: str
    size: int
    modality: Modality
    status: DocumentStatus
    analyzerId: str | None
    summary: str
    chunkCount: int
    visibility: Visibility
    createdAt: datetime
    updatedAt: datetime

    @classmethod
    def of(cls, doc: UserDocument) -> "UserDocumentSummary":
        return cls(
            id=doc.id,
            filename=doc.filename,
            contentType=doc.contentType,
            size=doc.size,
            modality=doc.modality,
            status=doc.status,
            analyzerId=doc.analyzerId,
            summary=doc.summary,
            chunkCount=doc.chunkCount,
            visibility=doc.visibility,
            createdAt=doc.createdAt,
            updatedAt=doc.updatedAt,
        )


class AnalyzerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    modalities: list[Modality] = Field(default_factory=lambda: [Modality.document])
    baseAnalyzerId: str | None = None
    config: dict = Field(default_factory=dict)


def _library(request: Request) -> DocumentLibraryRepository:
    """Return the library repo or 404 when the feature is disabled."""
    repo = getattr(request.app.state, "document_library", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The document library is not enabled.",
        )
    return repo


async def _block_disabled(request: Request, user_id: str) -> None:
    """Block only *disabled* accounts (403), mirroring the Phase 7C upload path:
    library bookkeeping is local work, so rate/budget limits don't apply here."""
    entitlements: EntitlementService = request.app.state.entitlements
    decision = await entitlements.check(user_id)
    if not decision.allowed and decision.code == status.HTTP_403_FORBIDDEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=decision.reason)


# --- documents ---
@router.get("/documents", response_model=list[UserDocumentSummary])
async def list_documents(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[UserDocumentSummary]:
    repo = _library(request)
    docs = await repo.list_documents(user.internal_user_id)
    return [UserDocumentSummary.of(d) for d in docs]


@router.get("/documents/{document_id}", response_model=UserDocumentSummary)
async def get_document(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserDocumentSummary:
    repo = _library(request)
    uid = user.internal_user_id
    try:
        doc = await repo.get_document(uid, document_id)
    except DocumentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if not can_access(uid, doc):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return UserDocumentSummary.of(doc)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    repo = _library(request)
    uid = user.internal_user_id
    try:
        doc = await repo.get_document(uid, document_id)
    except DocumentNotFoundError:
        return  # idempotent: already gone
    if not require_owner(uid, doc):
        # Never reveal another user's document via delete.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    await repo.delete_document(uid, document_id)


# --- analyzers ---
@router.get("/analyzers", response_model=list[Analyzer])
async def list_analyzers(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[Analyzer]:
    repo = _library(request)
    return await repo.list_analyzers(user.internal_user_id)


@router.get("/analyzers/{analyzer_id}", response_model=Analyzer)
async def get_analyzer(
    analyzer_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Analyzer:
    repo = _library(request)
    try:
        return await repo.get_analyzer(user.internal_user_id, analyzer_id)
    except AnalyzerNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analyzer not found")


@router.post(
    "/analyzers", response_model=Analyzer, status_code=status.HTTP_201_CREATED
)
async def create_analyzer(
    body: AnalyzerCreate,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Analyzer:
    repo = _library(request)
    uid = user.internal_user_id
    await _block_disabled(request, uid)
    analyzer = Analyzer(
        userId=uid,
        name=body.name,
        description=body.description,
        kind=AnalyzerKind.custom,
        modalities=body.modalities or [Modality.document],
        baseAnalyzerId=body.baseAnalyzerId,
        config=body.config,
    )
    try:
        created = await repo.create_analyzer(analyzer)
    except AnalyzerConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That analyzer id is reserved by a built-in analyzer.",
        )
    logger.info("library analyzer created user=%s id=%s", uid, created.id)
    return created


@router.delete(
    "/analyzers/{analyzer_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_analyzer(
    analyzer_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    repo = _library(request)
    try:
        await repo.delete_analyzer(user.internal_user_id, analyzer_id)
    except AnalyzerNotFoundError:
        # Built-ins aren't deletable; report not-found rather than 500.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analyzer not found")
