"""Per-user document library API.

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

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

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
from pydantic import BaseModel, Field, field_validator

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..entitlements.service import EntitlementService
from ..logging_setup import emit_custom_event
from ..memory.telemetry import emit_memory_operation
from ..content_understanding.models import (
    CU_SYNC_MAX_BYTES,
    CU_SYNC_MAX_PDF_PAGES,
    is_valid_analyzer_id,
)
from ..library.access import (
    can_access,
    list_accessible_documents,
    normalize_principal,
    require_owner,
)
from ..library.chunking import chunk_markdown
from ..library.compute_factory import DocumentComputeService
from ..library.blob_store import BlobNotFoundError
from ..library.ingest import DocumentIngestor, EnrichScheduleOutcome
from ..library.modality import classify_modality, normalize_content_type
from ..library.mistral_document import (
    MAX_MISTRAL_DOCUMENT_BYTES,
    MAX_MISTRAL_DOCUMENT_PAGES,
    pdf_page_count,
)
from ..library.models import (
    Analyzer,
    AnalyzerKind,
    AnalyzerOperation,
    AnalyzerProvider,
    BUILTIN_ANALYZERS,
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
    DocumentConflictError,
    DocumentLibraryRepository,
    DocumentNotFoundError,
)

logger = logging.getLogger(__name__)


def _analyzer_available(request: Request, analyzer: Analyzer) -> bool:
    settings = request.app.state.settings
    if analyzer.preview and not settings.cu_preview_enabled:
        return False
    if analyzer.id == "cu-agentic-document":
        return bool(settings.cu_agentic_analyzer_id)
    if analyzer.provider is AnalyzerProvider.mistral:
        return bool(
            analyzer.modelId
            and request.app.state.catalog.resolve_deployment(analyzer.modelId)
            is not None
        )
    return True
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
    analysisProvider: str | None = None
    analysisModel: str | None = None
    analysisVersion: str | None = None
    analysisPages: int | None = None
    analysisDeployment: str | None = None
    analysisRegion: str | None = None
    analysisSku: str | None = None
    analysisDataZone: str | None = None
    analysisResidency: str | None = None
    analysisApiVersion: str | None = None
    analysisOperation: str | None = None
    analysisWorkflow: str | None = None
    analysisCompletionModel: str | None = None
    analysisUsage: dict = Field(default_factory=dict)
    confidenceCount: int = 0
    groundedFieldCount: int = 0
    averageConfidence: float | None = None
    minimumConfidence: float | None = None
    contentFilterCount: int = 0
    analysisDetailsAvailable: bool = False
    summary: str
    chunkCount: int
    error: str | None = None
    citationReady: bool = False
    visibility: Visibility
    createdAt: datetime
    updatedAt: datetime
    versionCount: int = 0
    versions: list[DocumentVersionSummary] = Field(default_factory=list)

    @classmethod
    def of(cls, doc: UserDocument) -> "UserDocumentSummary":
        analysis = doc.analysis
        return cls(
            id=doc.id,
            filename=doc.filename,
            contentType=doc.contentType,
            size=doc.size,
            modality=doc.modality,
            status=doc.status,
            analyzerId=doc.analyzerId,
            analysisProvider=(
                analysis.provider if analysis is not None else doc.analysisProvider
            ),
            analysisModel=(
                analysis.model if analysis is not None else doc.analysisModel
            ),
            analysisVersion=(
                analysis.version if analysis is not None else doc.analysisVersion
            ),
            analysisPages=(
                analysis.pages if analysis is not None else doc.analysisPages
            ),
            analysisDeployment=(
                analysis.deployment
                if analysis is not None
                else doc.analysisDeployment
            ),
            analysisRegion=(
                analysis.region if analysis is not None else doc.analysisRegion
            ),
            analysisSku=(
                analysis.sku if analysis is not None else doc.analysisSku
            ),
            analysisDataZone=(
                analysis.dataZone if analysis is not None else doc.analysisDataZone
            ),
            analysisResidency=(
                analysis.residency
                if analysis is not None
                else doc.analysisResidency
            ),
            analysisApiVersion=analysis.apiVersion if analysis is not None else None,
            analysisOperation=analysis.operation if analysis is not None else None,
            analysisWorkflow=analysis.workflow if analysis is not None else None,
            analysisCompletionModel=(
                analysis.completionModel if analysis is not None else None
            ),
            analysisUsage=analysis.usage if analysis is not None else {},
            confidenceCount=analysis.confidenceCount if analysis is not None else 0,
            groundedFieldCount=(
                analysis.groundedFieldCount if analysis is not None else 0
            ),
            averageConfidence=(
                analysis.averageConfidence if analysis is not None else None
            ),
            minimumConfidence=(
                analysis.minimumConfidence if analysis is not None else None
            ),
            contentFilterCount=(
                analysis.contentFilterCount if analysis is not None else 0
            ),
            analysisDetailsAvailable=bool(doc.analysisPath),
            summary=doc.summary,
            chunkCount=doc.chunkCount,
            error=doc.error,
            citationReady=doc.status == DocumentStatus.ready,
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


class LibrarySummary(BaseModel):
    generatedAt: datetime
    status: str = "ok"
    total: int = 0
    byStatus: dict[str, int] = Field(default_factory=dict)
    byModality: dict[str, int] = Field(default_factory=dict)
    recent: list[UserDocumentSummary] = Field(default_factory=list)
    maxUploadBytes: int
    maxDocuments: int
    modalities: list[str] = Field(
        default_factory=lambda: ["document", "text", "image", "audio", "video"]
    )


# A custom analyzer's baseAnalyzerId is interpolated directly into the Content
# Understanding request URL path (see ContentUnderstandingClient.submit_url), so
# it is restricted to the same charset the CU service itself uses for analyzer
# ids. This blocks "/", "?", "#", whitespace, and control characters (including
# a trailing newline) from reaching URL construction. The rule lives in
# ``content_understanding.models`` so this validator and the CU client's own
# defense-in-depth check can never drift apart.


class AnalyzerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    modalities: list[Modality] = Field(default_factory=lambda: [Modality.document])
    baseAnalyzerId: str | None = None
    config: dict = Field(default_factory=dict)

    @field_validator("baseAnalyzerId")
    @classmethod
    def validate_base_analyzer_id(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_analyzer_id(value):
            raise ValueError(
                "baseAnalyzerId may only contain letters, digits, '.', '-' and "
                "'_' (1-64 characters)."
            )
        return value


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

    Gates the version-download endpoints behind the document compute flag: when document
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

    Gates the media endpoints (timeline + original-media stream): when the
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
    """Block only *disabled* accounts (403), mirroring the upload path:
    library bookkeeping is local work, so rate/budget limits don't apply here."""
    entitlements: EntitlementService = request.app.state.entitlements
    decision = await entitlements.check(user_id)
    if not decision.allowed and decision.code == status.HTTP_403_FORBIDDEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=decision.reason)


async def _accessible_document(
    request: Request, user: AuthenticatedUser, document_id: str
) -> UserDocument:
    """Load a document the caller may *access*, or raise 404.

    Resolves the caller's own document first; if they don't own it, falls back to
    a cross-owner lookup gated by :func:`can_access`, so a document shared with the
    caller's email (or tenant-public) resolves too. A missing or forbidden document
    returns a generic 404 (never leaks existence). The returned document's
    ``userId`` is the *owner* — callers that touch owner-scoped blobs/chunks must
    key on that, not on the caller's id."""
    repo = _library(request)
    uid = user.internal_user_id
    try:
        return await repo.get_document(uid, document_id)
    except DocumentNotFoundError:
        pass
    getter = getattr(repo, "get_by_id", None)
    doc = await getter((document_id or "").strip()) if getter is not None else None
    if doc is None or not can_access(uid, doc, email=user.email):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return doc


# --- documents ---
@router.get("/summary", response_model=LibrarySummary)
async def library_summary(
    request: Request,
    recent: int = 5,
    user: AuthenticatedUser = Depends(get_current_user),
) -> LibrarySummary:
    repo = _library(request)
    docs = await list_accessible_documents(
        repo, user.internal_user_id, email=user.email
    )
    docs.sort(key=lambda document: document.updatedAt, reverse=True)
    by_status: dict[str, int] = {}
    by_modality: dict[str, int] = {}
    for document in docs:
        by_status[document.status.value] = by_status.get(document.status.value, 0) + 1
        by_modality[document.modality.value] = by_modality.get(document.modality.value, 0) + 1
    settings = request.app.state.settings
    return LibrarySummary(
        generatedAt=datetime.now(timezone.utc),
        total=len(docs),
        byStatus=by_status,
        byModality=by_modality,
        recent=[
            UserDocumentSummary.of(document)
            for document in docs[: max(0, min(recent, 20))]
        ],
        maxUploadBytes=settings.document_max_upload_bytes,
        maxDocuments=settings.document_max_per_user,
    )


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
    started = time.monotonic()
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
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
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
    analyzer: Analyzer | None = None
    if analyzer_id:
        if analyzer_id in BUILTIN_ANALYZER_IDS:
            analyzer = next(
                item for item in BUILTIN_ANALYZERS if item.id == analyzer_id
            )
        else:
            try:
                analyzer = await repo.get_analyzer(uid, analyzer_id)
            except AnalyzerNotFoundError:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Analyzer not found",
                )

    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="File is too large.",
        )
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="File is empty."
        )

    content_type = normalize_content_type(file.content_type, file.filename)
    modality = classify_modality(content_type, file.filename or "document")
    if analyzer is not None and modality not in analyzer.modalities:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{analyzer.name} does not support {modality.value} files.",
        )
    if (
        analyzer is not None
        and analyzer.provider is AnalyzerProvider.mistral
        and len(data) > MAX_MISTRAL_DOCUMENT_BYTES
    ):
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Mistral document analysis supports files up to 30 MB.",
        )
    if analyzer is not None and not _analyzer_available(request, analyzer):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"{analyzer.name} is not available under this environment's "
                "model and data-residency policy."
            ),
        )
    if (
        analyzer is not None
        and analyzer.operation is AnalyzerOperation.synchronous
        and len(data) > CU_SYNC_MAX_BYTES
    ):
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Synchronous Content Understanding supports files up to 10 MB.",
        )
    if (
        analyzer is not None
        and analyzer.provider is AnalyzerProvider.mistral
        and (
            content_type.split(";", 1)[0].strip().lower() == "application/pdf"
            or (file.filename or "").lower().endswith(".pdf")
        )
    ):
        try:
            pages = await asyncio.to_thread(pdf_page_count, data)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        if pages > MAX_MISTRAL_DOCUMENT_PAGES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Mistral document analysis supports PDFs with at most "
                    f"{MAX_MISTRAL_DOCUMENT_PAGES} pages."
                ),
            )
    if (
        analyzer is not None
        and analyzer.operation is AnalyzerOperation.synchronous
        and (
            content_type.split(";", 1)[0].strip().lower() == "application/pdf"
            or (file.filename or "").lower().endswith(".pdf")
        )
    ):
        try:
            pages = await asyncio.to_thread(pdf_page_count, data)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        if pages > CU_SYNC_MAX_PDF_PAGES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Synchronous Content Understanding supports PDFs with at "
                    f"most {CU_SYNC_MAX_PDF_PAGES} pages."
                ),
            )
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
    if (
        not result.deduped
        and doc.status == DocumentStatus.stored
        and analyzer is not None
        and analyzer.operation is AnalyzerOperation.synchronous
    ):
        inline = await ingestor.enrich_inline(
            user_id=uid,
            document_id=doc.id,
            data=data,
            content_type=content_type,
        )
        if inline is EnrichScheduleOutcome.saturated:
            doc, settlement = await ingestor.settle_saturated(doc)
            if settlement != "committed":
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "The file was stored, but analysis capacity could not be "
                        "recorded. Retry the same upload shortly."
                    ),
                    headers={"Retry-After": "5"},
                )
        else:
            try:
                doc = await repo.get_document(uid, doc.id)
            except DocumentNotFoundError:
                # The owner deleted the document while the synchronous analyzer
                # was still running; enrich honoured the delete and purged it.
                # Report it as gone rather than raising an unhandled 500.
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Document not found",
                ) from None
    elif not result.deduped and doc.status == DocumentStatus.stored:
        scheduled = ingestor.schedule_enrich(
            user_id=uid,
            document_id=doc.id,
            content_type=content_type,
        )
        if scheduled is EnrichScheduleOutcome.saturated:
            doc, settlement = await ingestor.settle_saturated(doc)
            if settlement != "committed":
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "The file was stored, but analysis capacity could not be "
                        "recorded. Retry the same upload shortly."
                    ),
                    headers={"Retry-After": "5"},
                )
    logger.info(
        "library upload status=%s deduped=%s analyzer=%s",
        doc.status,
        result.deduped,
        analyzer_id or "automatic",
    )
    emit_custom_event(
        "document_ingest",
        {
            "status": doc.status.value,
            "modality": doc.modality.value,
            "contentType": doc.contentType,
            "size": doc.size,
            "deduped": result.deduped,
            "latencyMs": int((time.monotonic() - started) * 1000),
        },
    )
    return UserDocumentSummary.of(doc)


@router.get("/documents/{document_id}/analysis", response_model=dict)
async def get_document_analysis(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    repo = _library(request)
    # Deliberately NOT gated by ``_block_disabled``. That gate guards *spend*
    # (upload, memory writes, analyzer create); this is a pure owner read of
    # output that was already produced and already billed. Gating it alone
    # enforced no confidentiality boundary either — the raw document is still
    # downloadable via ``download_document_version`` and the derived scenes via
    # ``get_media_timeline``, both ungated — so it only broke the evidence
    # viewer for a disabled account. If a disabled account should lose library
    # access, gate every read together; do not re-add it to just this one.
    try:
        document = await repo.get_document(
            user.internal_user_id, document_id
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Analysis not found",
        ) from exc
    if document.status is not DocumentStatus.ready or not document.analysisPath:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Analysis not found",
        )
    try:
        raw = await _ingestor(request).blob.get(document.analysisPath)
        body = json.loads(raw)
    except (BlobNotFoundError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Analysis not found",
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Analysis not found",
        )
    return body


@router.get("/documents/{document_id}", response_model=UserDocumentSummary)
async def get_document(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserDocumentSummary:
    _library(request)
    doc = await _accessible_document(request, user, document_id)
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
    """List the "adjust & return" versions of a ready document.

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
    """Download a version's bytes, ownership- + ready-status-gated.

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
    """One analyzed audio/video segment's deep-link grounding: its time span plus
    the analyzer's keyframe and camera-shot boundaries (milliseconds)."""

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
    """Deep-link scene timeline for a ready audio/video document.

    Access- + ready-status-gated and restricted to audio/video: the owner, a
    grantee the document is shared with, or any authenticated user for a
    tenant-public document. A missing, forbidden, non-ready, or non-AV document
    returns a generic 404 (never leaks existence). A document with no scene detail
    returns an empty ``segments`` list."""
    retrieval = _retrieval(request)
    result = await retrieval.read_media_timeline(
        user.internal_user_id, document_id, email=user.email
    )
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

    Access- + ready-status-gated and restricted to audio/video (owner, grantee, or
    any authenticated user for a tenant-public document); the parsed/raw artifacts
    of non-AV documents are never exposed here. The browser fetches this with its
    bearer token (a raw ``<video src>`` could not), wraps the blob in an object URL,
    and seeks client-side — so the whole file is served as a single response. The
    bytes are read from the *owner's* blob path, so a shared document streams the
    owner's original. A missing/forbidden/non-ready/non-AV document returns a
    generic 404."""
    retrieval = _retrieval(request)
    result = await retrieval.read_media(
        user.internal_user_id, document_id, email=user.email
    )
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
    """Delete a document and cascade-forget any memories it contributed.

    Forgets memory before deleting the manifest (see the comment below) so a
    failed forget aborts the delete instead of losing the only retryable
    handle. See :func:`save_document_to_memory` for the residual save-vs-delete
    race this ordering narrows but does not close."""
    repo = _library(request)
    uid = user.internal_user_id
    try:
        doc = await repo.get_document(uid, document_id)
    except DocumentNotFoundError:
        return  # idempotent: already gone
    if not require_owner(uid, doc):
        # Never reveal another user's document via delete.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    # Forget anything this document contributed to the owner's durable memory
    # (save-to-memory) BEFORE deleting the manifest, and let a failure abort
    # the delete instead of swallowing it. Once the manifest is gone there is
    # no stable, retryable handle left to erase memories by document_id, so a
    # document whose erase failed here would leave its content permanently
    # recallable with no way to ever finish the job — the manifest delete is
    # NOT a safe "source of truth" for this step the way it is for the
    # best-effort blob/chunk purge below (recall is user-visible; a missing
    # blob/index entry is not). Mirrors the explicit 11E-3 forget endpoint
    # (memory.forget_document), which surfaces the same failures as 502.
    # A backend that cannot verify document-scoped hard deletion raises before
    # mutating anything. Keep the manifest/blob intact so the owner retains a
    # retryable handle instead of leaving orphaned, recallable memory.
    memory = getattr(request.app.state, "memory", None)
    if memory is not None and getattr(memory, "enabled", False):
        try:
            source_deleter = getattr(memory, "delete_document_source", None)
            if source_deleter is not None:
                forgotten = await source_deleter(uid, document_id)
            else:
                forgotten = await memory.forget_document(uid, document_id)
        except Exception:  # noqa: BLE001 - surface so the delete stays retryable
            logger.warning(
                "delete-cascaded memory-forget failed user=%s id=%s; aborting "
                "delete so the document remains a retryable handle",
                uid,
                document_id,
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not update memory right now; document was not deleted.",
            )
        if forgotten:
            logger.info(
                "delete cascaded memory-forget user=%s id=%s forgotten=%s",
                uid,
                document_id,
                forgotten,
            )

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
    """Promote a ready document's gist into the caller's durable memory.

    The explicit "save to memory and get it back" action: stores the document
    summary plus a bounded set of leading excerpts as ``kind="document"``
    memories, so the model can recall the document across sessions even when the
    library itself isn't queried. Flag-gated by the library (404 when document
    understanding is off) and by memory (409 when memory is disabled); owner-only
    and ``ready``-status-gated, mirroring the read/delete gates. Memory failures
    surface (502) because the user explicitly asked to save.

    Cosmos commits replacement memories with an active source marker in one
    transactional batch. Document deletion permanently tombstones that marker
    before purging, so a stale concurrent save cannot recreate orphaned memory."""
    started = time.monotonic()
    repo = _library(request)
    uid = user.internal_user_id
    await _block_disabled(request, uid)

    memory = request.app.state.memory
    if not getattr(memory, "enabled", False):
        emit_memory_operation("save", "disabled", "document_library", started)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Memory is not enabled."
        )

    try:
        doc = await repo.get_document(uid, document_id)
    except DocumentNotFoundError:
        emit_memory_operation("save", "not_found", "document_library", started)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if not require_owner(uid, doc):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if doc.status != DocumentStatus.ready:
        emit_memory_operation("save", "not_ready", "document_library", started)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document is not ready; it has no content to remember yet.",
        )

    items = await _document_memory_items(request, uid, doc)
    if not items:
        emit_memory_operation("save", "empty", "document_library", started)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
        emit_memory_operation("save", "failed", "document_library", started)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not save to memory right now.",
        )
    logger.info("save-to-memory user=%s id=%s saved=%s", uid, document_id, saved)
    emit_memory_operation("save", "ok", "document_library", started, count=saved)
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
    earlier can be forgotten regardless of its current status. Memory failures
    surface (502) because the user explicitly asked to forget.

    Cosmos uses a source marker and scoped purge so retries are safe."""
    started = time.monotonic()
    repo = _library(request)
    uid = user.internal_user_id
    await _block_disabled(request, uid)

    memory = request.app.state.memory
    if not getattr(memory, "enabled", False):
        emit_memory_operation("delete", "disabled", "document_library", started)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Memory is not enabled."
        )

    try:
        doc = await repo.get_document(uid, document_id)
    except DocumentNotFoundError:
        emit_memory_operation("delete", "not_found", "document_library", started)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if not require_owner(uid, doc):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    try:
        forgotten = await memory.forget_document(uid, document_id)
    except Exception:  # noqa: BLE001 - surface a transient memory failure, don't 500
        logger.warning(
            "forget-from-memory failed user=%s id=%s", uid, document_id, exc_info=True
        )
        emit_memory_operation("delete", "failed", "document_library", started)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not update memory right now.",
        )
    logger.info(
        "forget-from-memory user=%s id=%s forgotten=%s", uid, document_id, forgotten
    )
    emit_memory_operation(
        "delete", "ok", "document_library", started, count=forgotten
    )
    return ForgetFromMemoryResult(forgotten=forgotten)


# --- sharing ---
_MAX_GRANTEES = 100


def _valid_email(value: str) -> bool:
    """Lightweight grantee-email check: a normalized single-token address with a
    local part and a dotted domain. Intentionally permissive — the IdP is the real
    authority on who an email resolves to; this only rejects obvious junk so the
    ACL stays clean and a typo can't poison the grant list."""
    if not value or " " in value or value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")


class ShareState(BaseModel):
    """A document's sharing posture, surfaced to its owner."""

    documentId: str
    visibility: Visibility
    grantees: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, doc: UserDocument) -> "ShareState":
        return cls(documentId=doc.id, visibility=doc.visibility, grantees=list(doc.acl))


class ShareUpdate(BaseModel):
    """Owner request to replace a document's sharing posture. ``grantees`` are
    grantee emails; they only take effect for ``visibility == shared`` (cleared for
    ``private``/``public``, since those don't use the per-principal grant list)."""

    visibility: Visibility
    grantees: list[str] = Field(default_factory=list)


def _normalize_grantees(raw: list[str], owner_email: str | None) -> list[str]:
    """Normalize, validate, de-duplicate, and bound a grantee email list.

    Drops blanks, the owner's own email (self-share is implicit), and duplicates;
    rejects obviously malformed addresses (422); caps the list at
    ``_MAX_GRANTEES`` (422) so a single document can't accumulate an unbounded ACL.
    Order is preserved for a stable, reviewable grant list."""
    owner = normalize_principal(owner_email)
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw or []:
        principal = normalize_principal(entry)
        if not principal or principal == owner or principal in seen:
            continue
        if not _valid_email(principal):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"'{entry}' is not a valid email address.",
            )
        seen.add(principal)
        out.append(principal)
    if len(out) > _MAX_GRANTEES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"A document can be shared with at most {_MAX_GRANTEES} people.",
        )
    return out


@router.get("/shared", response_model=list[UserDocumentSummary])
async def list_shared_with_me(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[UserDocumentSummary]:
    """List documents explicitly shared *with* the caller.

    Scoped to ``visibility == shared`` documents whose ACL contains the caller's
    email — tenant-public documents are openable by id but deliberately not
    auto-listed here (scale + privacy). Returns ``[]`` when the caller has no email
    claim or the repository predates the sharing lookup. Owner-private artifacts
    (annotations, saved memories) never travel with these."""
    repo = _library(request)
    uid = user.internal_user_id
    principal = normalize_principal(user.email)
    if not principal:
        return []
    lookup = getattr(repo, "list_shared_with", None)
    if lookup is None:
        return []
    docs = await lookup(principal)
    return [UserDocumentSummary.of(d) for d in docs if d.userId != uid]


@router.get("/documents/{document_id}/shares", response_model=ShareState)
async def get_document_shares(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ShareState:
    """Read a document's sharing posture (owner-only).

    A missing or non-owned document returns a generic 404 (never leaks existence):
    only the owner can see or change who a document is shared with."""
    repo = _library(request)
    uid = user.internal_user_id
    try:
        doc = await repo.get_document(uid, document_id)
    except DocumentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if not require_owner(uid, doc):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return ShareState.of(doc)


@router.put("/documents/{document_id}/shares", response_model=ShareState)
async def set_document_shares(
    document_id: str,
    request: Request,
    payload: ShareUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ShareState:
    """Replace a document's sharing posture (owner-only).

    Sets ``visibility`` and, for ``shared``, the grantee email ACL (normalized,
    validated, de-duped, owner-skipped, capped). For ``private``/``public`` the ACL
    is cleared. A missing or non-owned document returns a generic 404. Annotations
    and saved memories stay owner-private and are never shared by this."""
    repo = _library(request)
    uid = user.internal_user_id
    try:
        doc = await repo.get_document(uid, document_id)
    except DocumentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if not require_owner(uid, doc):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    if payload.visibility == Visibility.shared:
        doc.acl = _normalize_grantees(payload.grantees, user.email)
    else:
        doc.acl = []
    doc.visibility = payload.visibility
    doc.touch()
    try:
        saved = await repo.update_document(doc)
    except DocumentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    except DocumentConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document changed concurrently; reload and try again.",
        )
    logger.info(
        "share-set user=%s id=%s visibility=%s grantees=%d",
        uid, document_id, doc.visibility.value, len(doc.acl),
    )
    return ShareState.of(saved)


@router.delete("/documents/{document_id}/shares/{email}", response_model=ShareState)
async def revoke_document_share(
    document_id: str,
    email: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ShareState:
    """Revoke one grantee's access to a shared document (owner-only).

    Idempotent: revoking an email that isn't on the ACL is a no-op that returns the
    current state. Leaves ``visibility`` untouched (the owner flips that via PUT);
    an emptied ACL simply grants no one besides the owner. A missing or non-owned
    document returns a generic 404."""
    repo = _library(request)
    uid = user.internal_user_id
    principal = normalize_principal(email)
    if not principal:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A grantee email is required.",
        )

    # Always perform an ETag-conditional replace, even when the first snapshot
    # does not contain the grantee. Returning that stale snapshot as success
    # could race with a concurrent grant. Conflicts are reloaded and retried;
    # after the accepted write, re-read and verify the current ACL is absent.
    for _attempt in range(3):
        try:
            doc = await repo.get_document(uid, document_id)
        except DocumentNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
            )
        if not require_owner(uid, doc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
            )

        was_present = principal in doc.acl
        if was_present:
            doc.acl = [entry for entry in doc.acl if entry != principal]
        doc.touch()
        try:
            await repo.update_document(doc)
        except DocumentNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
            )
        except DocumentConflictError:
            continue

        try:
            current = await repo.get_document(uid, document_id)
        except DocumentNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
            )
        if not require_owner(uid, current):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
            )
        if principal not in current.acl:
            if was_present:
                logger.info("share-revoke user=%s id=%s", uid, document_id)
            return ShareState.of(current)

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Document changed concurrently; reload and try again.",
    )


# --- annotations ---
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Annotation body must not be empty.",
        )
    annotation = DocumentAnnotation(body=text, anchor=_clean_anchor(body.anchor))
    doc.annotations.append(annotation)
    doc.touch()
    try:
        await repo.update_document(doc)
    except DocumentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    except DocumentConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document changed concurrently; reload and try again.",
        )
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
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Annotation body must not be empty.",
            )
        annotation.body = text
    if body.anchor is not None:
        annotation.anchor = _clean_anchor(body.anchor)
    annotation.touch()
    doc.touch()
    try:
        await repo.update_document(doc)
    except DocumentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    except DocumentConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document changed concurrently; reload and try again.",
        )
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
        try:
            await repo.update_document(doc)
        except DocumentNotFoundError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        except DocumentConflictError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Document changed concurrently; reload and try again.",
            )


# --- analyzers ---
@router.get("/analyzers", response_model=list[Analyzer])
async def list_analyzers(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[Analyzer]:
    repo = _library(request)
    analyzers = await repo.list_analyzers(user.internal_user_id)
    return [
        analyzer for analyzer in analyzers if _analyzer_available(request, analyzer)
    ]


@router.get("/analyzers/{analyzer_id}", response_model=Analyzer)
async def get_analyzer(
    analyzer_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Analyzer:
    repo = _library(request)
    try:
        analyzer = await repo.get_analyzer(user.internal_user_id, analyzer_id)
    except AnalyzerNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analyzer not found")
    if not _analyzer_available(request, analyzer):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analyzer not found")
    return analyzer


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
    logger.info("library analyzer created")
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
