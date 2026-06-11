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

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..entitlements.service import EntitlementService
from ..library.access import can_access, require_owner
from ..library.chunking import chunk_markdown
from ..library.compute_factory import DocumentComputeService
from ..library.ingest import DocumentIngestor
from ..library.models import (
    Analyzer,
    AnalyzerKind,
    BUILTIN_ANALYZER_IDS,
    DocumentAnnotation,
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


class DocumentVersionSummary(BaseModel):
    """An "adjust & return" version pointer surfaced to the client. Carries no
    blob path (downloads go through the gated version endpoint)."""

    version: int
    filename: str
    contentType: str
    size: int
    note: str
    createdAt: datetime


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
    versionCount: int = 0
    versions: list[DocumentVersionSummary] = Field(default_factory=list)

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
            versionCount=doc.version_count,
            versions=[
                DocumentVersionSummary(
                    version=v.n,
                    filename=v.filename,
                    contentType=v.contentType,
                    size=v.size,
                    note=v.note,
                    createdAt=v.createdAt,
                )
                for v in sorted(doc.versions, key=lambda v: v.n)
            ],
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


def _ingestor(request: Request) -> DocumentIngestor:
    """Return the ingest pipeline or 404 when document understanding is disabled."""
    ingestor = getattr(request.app.state, "document_ingestor", None)
    if ingestor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The document library is not enabled.",
        )
    return ingestor


def _compute(request: Request) -> "DocumentComputeService":
    """Return the document compute service or 404 when compute is disabled.

    Gates the version-download endpoints behind the Phase 11C flag: when document
    compute is off (default) there is no export path and no versions, so these
    routes refuse exactly like the library routes do when understanding is off."""
    compute = getattr(request.app.state, "document_compute", None)
    if compute is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document compute is not enabled.",
        )
    return compute


def _retrieval(request: Request):
    """Return the document retrieval service or 404 when understanding is disabled.

    Gates the Phase 11D media endpoints (timeline + original-media stream): when the
    library is off the retrieval service is never constructed, so these routes refuse
    exactly like the other library routes."""
    retrieval = getattr(request.app.state, "document_retrieval", None)
    if retrieval is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The document library is not enabled.",
        )
    return retrieval


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


@router.post(
    "/documents",
    response_model=UserDocumentSummary,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    analyzerId: str | None = Form(default=None),
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserDocumentSummary:
    """Ingest an upload into the user's library.

    Persists the bytes + creates the manifest synchronously (status ``stored``),
    then schedules Content Understanding enrichment as a tracked background task
    (so a subsequent delete can cancel an in-flight crack). Identical re-uploads
    (same bytes + analyzer) return the existing manifest without re-cracking.
    Flag-gated: 404 when document understanding is disabled.
    """
    repo = _library(request)
    ingestor = _ingestor(request)
    uid = user.internal_user_id
    await _block_disabled(request, uid)

    settings = request.app.state.settings
    max_bytes = settings.document_max_upload_bytes

    # Cheap pre-read guard: reject a wildly oversized body before spooling it.
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes * 2:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="File is too large.",
                )
        except ValueError:
            pass

    # Per-user retention cap (0 = unlimited).
    if settings.document_max_per_user > 0:
        existing = await repo.list_documents(uid)
        if len(existing) >= settings.document_max_per_user:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Your library is at the maximum of "
                    f"{settings.document_max_per_user} documents. "
                    "Remove one before adding another."
                ),
            )

    # Validate an explicit analyzer selection (built-in id or an owned custom one).
    analyzer_id = (analyzerId or "").strip() or None
    if analyzer_id and analyzer_id not in BUILTIN_ANALYZER_IDS:
        try:
            await repo.get_analyzer(uid, analyzer_id)
        except AnalyzerNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Analyzer not found"
            )

    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File is too large.",
        )
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="File is empty."
        )

    content_type = file.content_type or ""
    result = await ingestor.ingest(
        user_id=uid,
        filename=file.filename or "document",
        content_type=content_type,
        data=data,
        analyzer_id=analyzer_id,
    )
    doc = result.document
    # Schedule CU enrichment only for a freshly stored document (a dedupe hit is
    # already terminal). schedule_enrich is a no-op when CU is not configured and
    # tracks the task so a delete can cancel it mid-crack.
    if not result.deduped and doc.status == DocumentStatus.stored:
        ingestor.schedule_enrich(
            user_id=uid,
            document_id=doc.id,
            data=data,
            content_type=content_type,
        )
    logger.info(
        "library upload user=%s id=%s status=%s deduped=%s",
        uid, doc.id, doc.status, result.deduped,
    )
    return UserDocumentSummary.of(doc)


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


@router.get(
    "/documents/{document_id}/versions",
    response_model=list[DocumentVersionSummary],
)
async def list_document_versions(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[DocumentVersionSummary]:
    """List the "adjust & return" versions of a ready document (Phase 11C).

    Gated behind document compute (404 when off) and the export service's own
    ownership + ready-status gate; a missing, cross-user, or non-ready document
    returns a generic 404 (never leaks existence)."""
    compute = _compute(request)
    result = await compute.export.list_versions(user.internal_user_id, document_id)
    if "error" in result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return [
        DocumentVersionSummary(
            version=v["version"],
            filename=v["filename"],
            contentType=v["contentType"],
            size=v["size"],
            note=v["note"],
            createdAt=datetime.fromisoformat(v["createdAt"]),
        )
        for v in result["versions"]
    ]


@router.get("/documents/{document_id}/versions/{n}/content")
async def download_document_version(
    document_id: str,
    n: int,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Download a version's bytes (Phase 11C), ownership- + ready-status-gated.

    The original raw/parsed artifacts are never exposed here — only the additive
    versioned export blobs. A missing/cross-user/non-ready document or unknown
    version number returns a generic 404."""
    compute = _compute(request)
    result = await compute.export.read_version(user.internal_user_id, document_id, n)
    if "error" in result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    # ASCII-fold the (already path-stripped, printable-only) filename for the
    # latin-1 header transport so a unicode name can't raise on send.
    header_name = result["filename"].replace('"', "").encode("ascii", "ignore").decode() or "export.md"
    return Response(
        content=result["data"],
        media_type=result["contentType"] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{header_name}"'},
    )


class MediaTimelineSegment(BaseModel):
    """One analyzed audio/video segment's deep-link grounding (Phase 11D): its time
    span plus the analyzer's keyframe and camera-shot boundaries (milliseconds)."""

    index: int
    startMs: int | None = None
    endMs: int | None = None
    keyframes: list[int] = Field(default_factory=list)
    shots: list[int] = Field(default_factory=list)


class MediaTimeline(BaseModel):
    """Scene timeline for an audio/video document, consumed by the web player to
    render clickable scene/keyframe markers. ``segments`` is empty when the analyzer
    surfaced no scene detail (the media still plays, just without markers)."""

    documentId: str
    modality: str
    durationMs: int | None = None
    segments: list[MediaTimelineSegment] = Field(default_factory=list)


@router.get("/documents/{document_id}/timeline", response_model=MediaTimeline)
async def get_media_timeline(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MediaTimeline:
    """Deep-link scene timeline for a ready audio/video document (Phase 11D).

    Ownership- + ready-status-gated and restricted to audio/video; a missing,
    cross-user, non-ready, or non-AV document returns a generic 404 (never leaks
    existence). A document with no scene detail returns an empty ``segments`` list."""
    retrieval = _retrieval(request)
    result = await retrieval.read_media_timeline(user.internal_user_id, document_id)
    if "error" in result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return MediaTimeline(
        documentId=result["document_id"],
        modality=result["modality"],
        durationMs=result.get("durationMs"),
        segments=[MediaTimelineSegment(**s) for s in result["segments"]],
    )


@router.get("/documents/{document_id}/media")
async def stream_document_media(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Stream a ready audio/video document's ORIGINAL bytes for the deep-link player.

    Ownership- + ready-status-gated and restricted to audio/video; the parsed/raw
    artifacts of non-AV documents are never exposed here. The browser fetches this
    with its bearer token (a raw ``<video src>`` could not), wraps the blob in an
    object URL, and seeks client-side — so the whole file is served as a single
    response. A missing/cross-user/non-ready/non-AV document returns a generic 404."""
    retrieval = _retrieval(request)
    result = await retrieval.read_media(user.internal_user_id, document_id)
    if "error" in result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found")
    header_name = (
        result["filename"].replace('"', "").encode("ascii", "ignore").decode() or "media"
    )
    return Response(
        content=result["data"],
        media_type=result["content_type"] or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{header_name}"',
            "Cache-Control": "private, no-store",
        },
    )


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
    # Cancel any in-flight enrich for this document, then best-effort purge of
    # blob artifacts + indexed chunks. The manifest delete is the source of truth;
    # cancelling first shrinks the window where a racing crack could re-index, and
    # the enrich path itself re-checks existence so a late finish can't resurrect.
    ingestor = getattr(request.app.state, "document_ingestor", None)
    if ingestor is not None:
        await ingestor.cancel_enrich(uid, document_id)
        await ingestor.purge(uid, document_id)


class SaveToMemoryResult(BaseModel):
    """Result of promoting a document's gist into the caller's durable memory."""

    saved: int


class ForgetFromMemoryResult(BaseModel):
    """Result of removing a document's saved memories from the caller's store."""

    forgotten: int


async def _document_memory_items(
    request: Request, user_id: str, doc: UserDocument
) -> list[str]:
    """Bounded texts to remember for ``doc``: its summary plus leading parsed
    excerpts, capped by ``memory_document_max_items``.

    Best-effort on the excerpt read — a missing parsed blob, disabled retrieval,
    or any read error degrades to summary-only; this never raises."""
    settings = request.app.state.settings
    max_items = max(1, settings.memory_document_max_items)
    chunk_chars = max(1, settings.memory_document_chunk_chars)
    items: list[str] = []
    summary = (doc.summary or "").strip()
    if summary:
        items.append(summary)
    retrieval = getattr(request.app.state, "document_retrieval", None)
    if retrieval is not None and len(items) < max_items:
        content: str | None = None
        try:
            loaded = await retrieval.read_parsed(
                user_id, doc.id, max_chars=chunk_chars * max_items
            )
            if isinstance(loaded, dict):
                content = loaded.get("content")
        except Exception:  # noqa: BLE001 - excerpt sourcing is best-effort
            logger.warning(
                "save-to-memory excerpt read failed id=%s", doc.id, exc_info=True
            )
        if content:
            for chunk in chunk_markdown(content, max_chars=chunk_chars, overlap=0):
                if len(items) >= max_items:
                    break
                text = chunk.text.strip()
                if text and text not in items:
                    items.append(text)
    return items[:max_items]


@router.post(
    "/documents/{document_id}/memory",
    response_model=SaveToMemoryResult,
    status_code=status.HTTP_201_CREATED,
)
async def save_document_to_memory(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SaveToMemoryResult:
    """Promote a ready document's gist into the caller's durable memory (Phase 11E-1).

    The explicit "save to memory and get it back" action: stores the document
    summary plus a bounded set of leading excerpts as ``kind="document"``
    memories, so the model can recall the document across sessions even when the
    library itself isn't queried. Flag-gated by the library (404 when document
    understanding is off) and by memory (409 when memory is disabled); owner-only
    and ``ready``-status-gated, mirroring the read/delete gates. Memory failures
    surface (502) because the user explicitly asked to save."""
    repo = _library(request)
    uid = user.internal_user_id
    await _block_disabled(request, uid)

    memory = request.app.state.memory
    if not getattr(memory, "enabled", False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Memory is not enabled."
        )

    try:
        doc = await repo.get_document(uid, document_id)
    except DocumentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if not can_access(uid, doc):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if doc.status != DocumentStatus.ready:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document is not ready; it has no content to remember yet.",
        )

    items = await _document_memory_items(request, uid, doc)
    if not items:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Document has no content to remember.",
        )
    try:
        saved = await memory.remember_document(
            uid, items=items, session_id=None, document_id=document_id
        )
    except Exception:  # noqa: BLE001 - surface a transient memory failure, don't 500
        logger.warning(
            "save-to-memory failed user=%s id=%s", uid, document_id, exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not save to memory right now.",
        )
    logger.info("save-to-memory user=%s id=%s saved=%s", uid, document_id, saved)
    return SaveToMemoryResult(saved=saved)


@router.delete(
    "/documents/{document_id}/memory",
    response_model=ForgetFromMemoryResult,
)
async def forget_document_from_memory(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ForgetFromMemoryResult:
    """Remove a document's saved memories from the caller's durable store (11E-3).

    The explicit undo of :func:`save_document_to_memory`: forgets exactly the
    memories saved from this document, leaving chat-sourced and other documents'
    memories intact. Flag-gated by the library (404 when document understanding
    is off) and by memory (409 when memory is disabled); owner-only. Unlike save
    there is no ``ready``-status gate — a document whose memories were saved
    earlier can be forgotten regardless of its current status. Idempotent: a
    document with nothing saved forgets ``0``. Memory failures surface (502)
    because the user explicitly asked to forget."""
    repo = _library(request)
    uid = user.internal_user_id
    await _block_disabled(request, uid)

    memory = request.app.state.memory
    if not getattr(memory, "enabled", False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Memory is not enabled."
        )

    try:
        doc = await repo.get_document(uid, document_id)
    except DocumentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if not can_access(uid, doc):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    try:
        forgotten = await memory.forget_document(uid, document_id)
    except Exception:  # noqa: BLE001 - surface a transient memory failure, don't 500
        logger.warning(
            "forget-from-memory failed user=%s id=%s", uid, document_id, exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not update memory right now.",
        )
    logger.info(
        "forget-from-memory user=%s id=%s forgotten=%s", uid, document_id, forgotten
    )
    return ForgetFromMemoryResult(forgotten=forgotten)


# --- annotations (Phase 11E-2) ---
_MAX_ANNOTATION_BODY = 4000
_MAX_ANNOTATION_ANCHOR = 200


def _clean_body(value: str) -> str:
    """Sanitize an annotation body: drop control characters except newlines and
    tabs, normalize line endings, and trim. Multi-line is allowed (notes)."""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "".join(
        ch for ch in text if ch in ("\n", "\t") or ord(ch) >= 32
    )
    return cleaned.strip()


def _clean_anchor(value: str) -> str:
    """Sanitize an annotation anchor to a single trimmed line (no control chars)."""
    cleaned = "".join(ch for ch in value if ord(ch) >= 32)
    return cleaned.strip()


class AnnotationCreate(BaseModel):
    body: str = Field(min_length=1, max_length=_MAX_ANNOTATION_BODY)
    anchor: str = Field(default="", max_length=_MAX_ANNOTATION_ANCHOR)


class AnnotationUpdate(BaseModel):
    body: str | None = Field(default=None, max_length=_MAX_ANNOTATION_BODY)
    anchor: str | None = Field(default=None, max_length=_MAX_ANNOTATION_ANCHOR)


class AnnotationView(BaseModel):
    """An annotation surfaced to the client (mirrors DocumentAnnotation)."""

    id: str
    body: str
    anchor: str
    createdAt: datetime
    updatedAt: datetime

    @classmethod
    def of(cls, a: DocumentAnnotation) -> "AnnotationView":
        return cls(
            id=a.id,
            body=a.body,
            anchor=a.anchor,
            createdAt=a.createdAt,
            updatedAt=a.updatedAt,
        )


async def _owned_document(request: Request, user_id: str, document_id: str) -> UserDocument:
    """Load a document and require ownership, or raise a generic 404.

    Annotations are owner-only (create/read/update/delete) — even after read
    sharing is enabled later, notes stay private to the owner — so this uses
    ``require_owner`` rather than ``can_access``."""
    repo = _library(request)
    try:
        doc = await repo.get_document(user_id, document_id)
    except DocumentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if not require_owner(user_id, doc):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return doc


@router.get(
    "/documents/{document_id}/annotations",
    response_model=list[AnnotationView],
)
async def list_annotations(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[AnnotationView]:
    """List the owner's notes on a document, oldest first."""
    doc = await _owned_document(request, user.internal_user_id, document_id)
    ordered = sorted(doc.annotations, key=lambda a: a.createdAt)
    return [AnnotationView.of(a) for a in ordered]


@router.post(
    "/documents/{document_id}/annotations",
    response_model=AnnotationView,
    status_code=status.HTTP_201_CREATED,
)
async def create_annotation(
    document_id: str,
    body: AnnotationCreate,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AnnotationView:
    """Pin a note to a document. Owner-only; body/anchor are sanitized + capped."""
    repo = _library(request)
    uid = user.internal_user_id
    doc = await _owned_document(request, uid, document_id)
    text = _clean_body(body.body)
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Annotation body must not be empty.",
        )
    annotation = DocumentAnnotation(body=text, anchor=_clean_anchor(body.anchor))
    doc.annotations.append(annotation)
    doc.touch()
    await repo.update_document(doc)
    logger.info("annotation created user=%s doc=%s id=%s", uid, document_id, annotation.id)
    return AnnotationView.of(annotation)


@router.patch(
    "/documents/{document_id}/annotations/{annotation_id}",
    response_model=AnnotationView,
)
async def update_annotation(
    document_id: str,
    annotation_id: str,
    body: AnnotationUpdate,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AnnotationView:
    """Edit a note's body and/or anchor in place. Owner-only."""
    repo = _library(request)
    uid = user.internal_user_id
    doc = await _owned_document(request, uid, document_id)
    annotation = next((a for a in doc.annotations if a.id == annotation_id), None)
    if annotation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Annotation not found"
        )
    if body.body is not None:
        text = _clean_body(body.body)
        if not text:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Annotation body must not be empty.",
            )
        annotation.body = text
    if body.anchor is not None:
        annotation.anchor = _clean_anchor(body.anchor)
    annotation.touch()
    doc.touch()
    await repo.update_document(doc)
    return AnnotationView.of(annotation)


@router.delete(
    "/documents/{document_id}/annotations/{annotation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_annotation(
    document_id: str,
    annotation_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Remove a note. Owner-only and idempotent (a missing note still 204s)."""
    repo = _library(request)
    uid = user.internal_user_id
    doc = await _owned_document(request, uid, document_id)
    before = len(doc.annotations)
    doc.annotations = [a for a in doc.annotations if a.id != annotation_id]
    if len(doc.annotations) != before:
        doc.touch()
        await repo.update_document(doc)


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
