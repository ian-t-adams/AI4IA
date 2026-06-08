"""Per-session document upload, listing, and deletion (Phase 7C).

Uploaded files are parsed to plain text locally (no model call) and stored as
session-scoped, ownership-checked documents. Their text is later injected
(capped) into chat turns as untrusted reference context (see ``routers/chat.py``).

Governance: extraction is local and cheap, so uploads are NOT rate-limited or
metered like model turns — only an explicitly *disabled* account is blocked.
Caps (file size, per-session document count) bound resource use.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..documents.extract import (
    EmptyDocumentError,
    UnsupportedDocumentError,
    extract_text,
)
from ..documents.extract import DocumentError
from ..entitlements.service import EntitlementService
from ..sessions.models import Document
from ..sessions.repository import SessionNotFoundError, SessionRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sessions", tags=["documents"])

# Max bytes accepted per upload (read with a +1 probe to detect overflow).
MAX_UPLOAD_BYTES = 10_000_000
# Max documents retained per session (keeps the chat-context budget meaningful).
MAX_DOCS_PER_SESSION = 8
# Reject obviously oversized request bodies early (before the body is fully
# spooled) using Content-Length. Generous slack over MAX_UPLOAD_BYTES covers
# multipart framing; the precise 10 MB limit is still enforced on read.
HARD_BODY_CEILING = MAX_UPLOAD_BYTES * 2
# Length of the inline text preview returned in summaries.
PREVIEW_CHARS = 240


class DocumentSummary(BaseModel):
    """Document metadata returned to the client (never the full extracted text)."""

    id: str
    sessionId: str
    filename: str
    contentType: str
    size: int
    charCount: int
    truncated: bool
    preview: str
    createdAt: datetime

    @classmethod
    def of(cls, doc: Document) -> "DocumentSummary":
        return cls(
            id=doc.id,
            sessionId=doc.sessionId,
            filename=doc.filename,
            contentType=doc.contentType,
            size=doc.size,
            charCount=doc.charCount,
            truncated=doc.truncated,
            preview=doc.text[:PREVIEW_CHARS],
            createdAt=doc.createdAt,
        )


def _repo(request: Request) -> SessionRepository:
    return request.app.state.session_repo


async def _require_session(repo: SessionRepository, user_id: str, session_id: str) -> None:
    try:
        await repo.get_session(user_id, session_id)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )


async def _block_disabled(request: Request, user_id: str) -> None:
    """Block only *disabled* accounts. Uploads are local work, so rate/budget
    limits (429) deliberately don't apply — only an admin disable (403) does."""
    entitlements: EntitlementService = request.app.state.entitlements
    decision = await entitlements.check(user_id)
    if not decision.allowed and decision.code == status.HTTP_403_FORBIDDEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=decision.reason)


def _safe_filename(name: str | None) -> str:
    base = (name or "document").replace("\\", "/").split("/")[-1]
    base = "".join(c for c in base if c.isprintable()).strip()
    return (base or "document")[:200]


@router.post(
    "/{session_id}/documents",
    response_model=DocumentSummary,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    session_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DocumentSummary:
    repo = _repo(request)
    uid = user.internal_user_id
    await _require_session(repo, uid, session_id)
    await _block_disabled(request, uid)

    # Cheap pre-read guard: reject a wildly oversized body before reading it.
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > HARD_BODY_CEILING:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="File is too large (max 10 MB).",
                )
        except ValueError:
            pass

    existing = await repo.list_documents(uid, session_id)
    if len(existing) >= MAX_DOCS_PER_SESSION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This chat already has the maximum of {MAX_DOCS_PER_SESSION} "
                "documents. Remove one before adding another."
            ),
        )

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File is too large (max 10 MB).",
        )
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File is empty.",
        )

    try:
        text, truncated = await extract_text(
            file.filename or "document", file.content_type or "", data
        )
    except UnsupportedDocumentError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        )
    except EmptyDocumentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except DocumentError as exc:
        # Corrupt / mismatched-type files.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )

    document = Document(
        sessionId=session_id,
        userId=uid,
        filename=_safe_filename(file.filename),
        contentType=_base_contenttype(file.content_type),
        size=len(data),
        charCount=len(text),
        truncated=truncated,
        text=text,
    )
    await repo.add_document(uid, document)
    logger.info(
        "document uploaded session=%s id=%s chars=%s truncated=%s",
        session_id, document.id, document.charCount, document.truncated,
    )
    return DocumentSummary.of(document)


@router.get("/{session_id}/documents", response_model=list[DocumentSummary])
async def list_documents(
    session_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[DocumentSummary]:
    repo = _repo(request)
    uid = user.internal_user_id
    await _require_session(repo, uid, session_id)
    docs = await repo.list_documents(uid, session_id)
    return [DocumentSummary.of(d) for d in docs]


@router.delete(
    "/{session_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document(
    session_id: str,
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    repo = _repo(request)
    uid = user.internal_user_id
    await _require_session(repo, uid, session_id)
    await repo.delete_document(uid, session_id, document_id)


def _base_contenttype(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip()[:128]
