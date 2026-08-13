"""Document ingest orchestrator.

Turns an upload into a manifest + retrievable chunks, governed and fail-soft:

1. :meth:`ingest` (sync, on the request path) — hash + content-dedupe, persist the
   raw bytes to blob, derive an instant quick-text summary (inline extractor,
   best-effort), and create the manifest at status ``stored``. Returns immediately
   so the upload feels fast; if the same bytes+analyzer were ingested before it
   returns the existing manifest (no re-crack).
2. :meth:`enrich` (async, scheduled as a background task) — run Content
   Understanding, persist ``parsed.md``, chunk + embed into the per-user
   ``doc_chunks`` vector store, write a ``chunks.jsonl`` sidecar, then flip the
   manifest to ``ready`` (or ``failed`` with the quick-text fallback retained).

Governance: one analyzer operation is metered per enrich attempt. Content
Understanding remains cost-unknown because its billing dimensions are not exposed
here; Mistral records returned page count against its catalog deployment. Embeddings
are not separately metered (consistent with memory). ``enrich`` never raises.

All IO is injected (blob store, CU client, embedder, chunk store), so the
orchestrator is unit-tested end to end without network or Azure SDKs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from ..catalog import DeploymentOption
from ..config import Settings
from ..content_understanding.models import CUResult
from ..agents.tools import redact
from ..documents.extract import DocumentError, extract_text
from ..memory.embedder import GatewayEmbedder
from ..logging_setup import emit_custom_event
from ..usage.models import TokenUsage, UsageTarget
from ..usage.service import UsageService
from .blob_store import CHUNKS_NAME, MEDIA_NAME, PARSED_NAME, RAW_NAME, BlobStore, blob_path
from .chunking import chunk_audiovisual, chunk_markdown, media_timeline
from .doc_chunks import DocChunkRecord, DocChunkStore
from .hashing import content_hash
from .modality import classify_modality
from .models import (
    BUILTIN_ANALYZERS,
    BUILTIN_ANALYZER_IDS,
    Analyzer,
    AnalyzerProvider,
    DocumentAnalysis,
    DocumentStatus,
    Modality,
    UserDocument,
)
from .repository import (
    AnalyzerNotFoundError,
    DocumentLibraryRepository,
    DocumentNotFoundError,
)

logger = logging.getLogger(__name__)

# session_id / model_id stamped on the metered CU operation (the ledger groups by
# these the same way chat turns group by session + model).
_INGEST_SESSION = "document-ingest"
_CU_MODEL_ID = "content-understanding"
# Max characters of the one-line summary card surfaced in the library list.
_SUMMARY_LIMIT = 240
MAX_CONCURRENT_DOCUMENT_ENRICHMENTS = 4
MAX_PENDING_DOCUMENT_ENRICHMENTS = 32

_ENRICHMENT_GATES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_ENRICHMENT_TASKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, set[asyncio.Task[None]]
] = weakref.WeakKeyDictionary()


def _global_enrichment_state() -> tuple[asyncio.Semaphore, set[asyncio.Task[None]]]:
    loop = asyncio.get_running_loop()
    gate = _ENRICHMENT_GATES.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(MAX_CONCURRENT_DOCUMENT_ENRICHMENTS)
        _ENRICHMENT_GATES[loop] = gate
    tasks = _ENRICHMENT_TASKS.get(loop)
    if tasks is None:
        tasks = set()
        _ENRICHMENT_TASKS[loop] = tasks
    return gate, tasks


@dataclass(slots=True)
class IngestResult:
    document: UserDocument
    # True when an identical (bytes, analyzer) upload already existed — no work
    # was scheduled, the existing manifest is returned as-is.
    deduped: bool


class EnrichScheduleOutcome(str, Enum):
    scheduled = "scheduled"
    already_running = "already_running"
    disabled = "disabled"
    closed = "closed"
    saturated = "saturated"


def resolve_cu_analyzer_id(
    analyzer: Analyzer | None, modality: str, settings: Settings
) -> str:
    """Concrete CU analyzer id to crack with.

    - No analyzer (default) or a built-in → the per-modality prebuilt default.
    - A custom analyzer → its ``baseAnalyzerId`` if set, else the modality default.
    """
    if analyzer is not None and not analyzer.builtin and analyzer.baseAnalyzerId:
        return analyzer.baseAnalyzerId
    return settings.cu_analyzer_for_modality(modality)


def summarize_markdown(markdown: str, *, limit: int = _SUMMARY_LIMIT) -> str:
    """First meaningful line of the parse, trimmed, as a one-line summary card."""
    for raw in (markdown or "").splitlines():
        line = raw.strip().lstrip("#").strip()
        if line:
            return line[:limit]
    return ""


def _sidecar_row(chunk) -> dict:
    """One ``chunks.jsonl`` record. Always carries the document grounding keys;
    audio/video time grounding (startMs/endMs/speaker/segment) is added only when
    present, so the document sidecar shape is unchanged."""
    row = {
        "index": chunk.index,
        "text": chunk.text,
        "heading": chunk.grounding.get("heading"),
        "charStart": chunk.grounding.get("charStart"),
        "charEnd": chunk.grounding.get("charEnd"),
    }
    for key in ("startMs", "endMs", "speaker", "segment"):
        value = chunk.grounding.get(key)
        if value is not None:
            row[key] = value
    return row


def _extension(filename: str) -> str:
    parts = (filename or "").rsplit(".", 1)
    if len(parts) == 2 and 1 <= len(parts[1]) <= 12 and parts[1].isalnum():
        return "." + parts[1].lower()
    return ""


def _safe_filename(name: str | None) -> str:
    base = (name or "document").replace("\\", "/").split("/")[-1]
    base = "".join(c for c in base if c.isprintable()).strip()
    return (base or "document")[:200]


def _base_content_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip()[:128]


class DocumentIngestor:
    def __init__(
        self,
        *,
        library: DocumentLibraryRepository,
        blob_store: BlobStore,
        settings: Settings,
        usage: UsageService,
        cu_client=None,
        mistral_client=None,
        embedder: GatewayEmbedder | None = None,
        chunk_store: DocChunkStore | None = None,
    ) -> None:
        self._library = library
        self._blob = blob_store
        self._settings = settings
        self._usage = usage
        self._cu = cu_client
        self._mistral = mistral_client
        self._embedder = embedder
        self._chunks = chunk_store
        # In-flight enrich tasks keyed by (user_id, document_id) so a delete can
        # cancel the racing enrich and shutdown can drain them. Bounds the
        # delete-during-enrich resurrection window to near-zero; correctness does
        # not depend on cancellation timing (the manifest re-check + non-
        # resurrecting terminal write make a late finish lose to the delete).
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._closed = False

    @property
    def cu_enabled(self) -> bool:
        return self._cu is not None

    # Read-only accessors so the retrieval consumer (11B-2) can share the SAME
    # backing IO instances the producer writes through. This matters for the
    # in-memory stores used in local/dev/tests: a document indexed into the
    # ingestor's in-process chunk/blob store would be invisible to retrieval if
    # the consumer built its own. Sharing keeps a single source of IO truth and
    # guarantees producer/consumer parity.
    @property
    def library(self) -> DocumentLibraryRepository:
        return self._library

    @property
    def blob(self) -> BlobStore:
        return self._blob

    @property
    def chunks(self) -> DocChunkStore | None:
        return self._chunks

    @property
    def embedder(self) -> GatewayEmbedder | None:
        return self._embedder

    def schedule_enrich(
        self,
        *,
        user_id: str,
        document_id: str,
        content_type: str,
    ) -> EnrichScheduleOutcome:
        """Fire-and-forget :meth:`enrich`, tracked so delete/shutdown can cancel.

        A no-op when CU is not configured (the document simply settles at
        ``stored``) or after :meth:`close`. Replaces an untracked
        ``BackgroundTasks.add_task`` so an in-flight crack can be cancelled the
        moment the user deletes the document.
        """
        if self._cu is None and self._mistral is None:
            return EnrichScheduleOutcome.disabled
        if self._closed:
            return EnrichScheduleOutcome.closed
        key = (user_id, document_id)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            return EnrichScheduleOutcome.already_running
        _, global_tasks = _global_enrichment_state()
        if len(global_tasks) >= MAX_PENDING_DOCUMENT_ENRICHMENTS:
            logger.warning(
                "document enrichment queue saturated; leaving stored id=%s",
                document_id,
            )
            emit_custom_event(
                "document_ingest_saturated",
                {
                    "stage": "admission",
                    "pending": len(global_tasks),
                    "limit": MAX_PENDING_DOCUMENT_ENRICHMENTS,
                },
            )
            return EnrichScheduleOutcome.saturated
        task = asyncio.create_task(
            self._run_scheduled_enrich(
                user_id=user_id,
                document_id=document_id,
                content_type=content_type,
            )
        )
        self._tasks[key] = task
        global_tasks.add(task)

        def _done(t: asyncio.Task[None], _key=key) -> None:
            if self._tasks.get(_key) is t:
                self._tasks.pop(_key, None)
            global_tasks.discard(t)
            if not t.cancelled():
                # enrich() never raises, but retrieve any exception so it is not
                # reported as "never retrieved".
                exc = t.exception()
                if exc is not None:  # pragma: no cover - defensive
                    logger.warning("enrich task errored: %s", exc, exc_info=exc)

        task.add_done_callback(_done)
        return EnrichScheduleOutcome.scheduled

    async def _run_scheduled_enrich(
        self, *, user_id: str, document_id: str, content_type: str
    ) -> None:
        gate, _ = _global_enrichment_state()
        if gate.locked():
            emit_custom_event(
                "document_ingest_saturated",
                {
                    "stage": "concurrency",
                    "limit": MAX_CONCURRENT_DOCUMENT_ENRICHMENTS,
                },
            )
        async with gate:
            await self.enrich(
                user_id=user_id,
                document_id=document_id,
                data=None,
                content_type=content_type,
            )

    async def settle_saturated(self, doc: UserDocument) -> tuple[UserDocument, str]:
        """Make an admission-rejected upload terminal and retryable."""
        outcome, updated = await self._safe_update(
            doc,
            {
                "status": DocumentStatus.failed,
                "error": (
                    "Analysis capacity is temporarily full. Re-upload to retry."
                ),
            },
            require_status=DocumentStatus.stored,
        )
        if outcome == "committed" and updated is not None:
            return updated, outcome
        return doc, outcome

    async def cancel_enrich(self, user_id: str, document_id: str) -> None:
        """Cancel and drain the in-flight enrich for a document, if any."""
        task = self._tasks.pop((user_id, document_id), None)
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _, global_tasks = _global_enrichment_state()
        global_tasks.discard(task)

    async def ingest(
        self,
        *,
        user_id: str,
        filename: str,
        content_type: str,
        data: bytes,
        analyzer_id: str | None = None,
    ) -> IngestResult:
        """Persist bytes + create the manifest (status ``stored``). Idempotent on
        (content-hash, analyzer): a repeat upload returns the existing manifest."""
        digest = content_hash(data)
        existing = await self._library.find_by_dedupe_key(user_id, digest, analyzer_id)
        if existing is not None:
            if (
                self._cu is not None or self._mistral is not None
            ) and existing.status == DocumentStatus.stored:
                # A prior admission failure may have persisted the raw upload but
                # failed to mark it terminal. Re-drive scheduling on identical
                # upload; schedule_enrich deduplicates an already-running task.
                return IngestResult(document=existing, deduped=False)
            if existing.status == DocumentStatus.failed:
                # A failed row is not a successful dedupe hit. The old behavior
                # returned it forever, so the UI's own "Re-upload to retry"
                # recovery message could never trigger another enrich.
                raw_path = existing.rawPath or blob_path(
                    user_id, existing.id, f"{RAW_NAME}{_extension(filename)}"
                )
                await self._blob.put(raw_path, data, _base_content_type(content_type) or None)
                changes: dict[str, object] = {
                    "filename": _safe_filename(filename),
                    "contentType": _base_content_type(content_type),
                    "size": len(data),
                    "rawPath": raw_path,
                    "parsedPath": None,
                    "chunksPath": None,
                    "chunkCount": 0,
                    "status": DocumentStatus.stored,
                    "error": None,
                }
                outcome, reset = await self._safe_update(
                    existing, changes, require_status=DocumentStatus.failed
                )
                if outcome == "committed" and reset is not None:
                    return IngestResult(document=reset, deduped=False)
                if reset is not None:
                    return IngestResult(document=reset, deduped=True)
            return IngestResult(document=existing, deduped=True)

        modality = classify_modality(content_type, filename)
        doc = UserDocument(
            userId=user_id,
            filename=_safe_filename(filename),
            contentType=_base_content_type(content_type),
            size=len(data),
            contentHash=digest,
            modality=modality,
            analyzerId=analyzer_id,
            status=DocumentStatus.pending,
        )

        raw_path = blob_path(user_id, doc.id, f"{RAW_NAME}{_extension(filename)}")
        await self._blob.put(raw_path, data, doc.contentType or None)
        doc.rawPath = raw_path

        # Instant value: a cheap local text extract for the summary card while CU
        # runs. Best-effort — unsupported/binary modalities just skip it.
        try:
            text, _ = await extract_text(doc.filename, content_type or "", data)
            doc.summary = summarize_markdown(text)
        except DocumentError:
            pass
        except Exception:  # noqa: BLE001 - the quick summary must never block upload
            logger.debug("quick-text summary failed", exc_info=True)

        doc.status = DocumentStatus.stored
        created = await self._library.create_document(doc)
        return IngestResult(document=created, deduped=False)

    async def _resolve_analyzer(
        self, user_id: str, analyzer_id: str | None
    ) -> Analyzer | None:
        if not analyzer_id:
            return None
        if analyzer_id in BUILTIN_ANALYZER_IDS:
            return next(
                analyzer for analyzer in BUILTIN_ANALYZERS if analyzer.id == analyzer_id
            )
        try:
            return await self._library.get_analyzer(user_id, analyzer_id)
        except AnalyzerNotFoundError:
            return None

    async def enrich(
        self,
        *,
        user_id: str,
        document_id: str,
        data: bytes | None,
        content_type: str,
    ) -> None:
        """Crack with CU, index chunks, and flip the manifest to ready/failed.

        Scheduled as a background task; never raises. A no-op (leaves the document
        at ``stored``) when CU is not configured.
        """
        try:
            doc = await self._library.get_document(user_id, document_id)
        except DocumentNotFoundError:
            logger.warning("enrich: document gone user=%s id=%s", user_id, document_id)
            return

        modality = doc.modality.value if isinstance(doc.modality, Modality) else str(doc.modality)
        started = time.monotonic()
        analyzer = await self._resolve_analyzer(user_id, doc.analyzerId)
        provider = (
            analyzer.provider
            if analyzer is not None
            else AnalyzerProvider.content_understanding
        )
        mistral_client = self._mistral
        cu_client = self._cu
        if provider is AnalyzerProvider.mistral and mistral_client is None:
            return
        if provider is AnalyzerProvider.content_understanding and cu_client is None:
            return
        analysis_model = (
            analyzer.modelId
            if provider is AnalyzerProvider.mistral and analyzer is not None
            else resolve_cu_analyzer_id(analyzer, modality, self._settings)
        )
        if not analysis_model:
            return
        analysis_version = analyzer.modelVersion if analyzer is not None else None
        analysis_deployment: DeploymentOption | None = None
        analysis_pages: int | None = None
        provider_completed = False

        initial_outcome, committed = await self._safe_update(
            doc,
            {"status": DocumentStatus.analyzing, "error": None},
            require_status=DocumentStatus.stored,
        )
        if initial_outcome != "committed" or committed is None:
            emit_custom_event(
                "document_ingest_terminal",
                {
                    "status": (
                        "cancelled" if initial_outcome == "missing" else "failed"
                    ),
                    "modality": modality,
                    "stage": "manifest_start",
                    "persistenceOutcome": initial_outcome,
                    "latencyMs": int((time.monotonic() - started) * 1000),
                },
            )
            return
        doc = committed

        meter_status = "complete"
        deleted_mid_flight = False
        terminal_status: str | None = None
        persistence_outcome = "not_attempted"
        try:
            payload = data
            data = None
            if payload is None:
                if not doc.rawPath:
                    raise RuntimeError("canonical raw document is unavailable")
                payload = await self._blob.get(doc.rawPath)
            try:
                if provider is AnalyzerProvider.mistral:
                    assert mistral_client is not None
                    try:
                        result, analysis_deployment = await mistral_client.analyze(
                            analysis_model,
                            payload,
                            content_type or "application/octet-stream",
                        )
                    except Exception as exc:
                        if getattr(exc, "provider_completed", False):
                            provider_completed = True
                            analysis_deployment = getattr(exc, "deployment", None)
                            analysis_pages = getattr(exc, "pages", None)
                        raise
                else:
                    assert cu_client is not None
                    result = await cu_client.analyze(
                        analysis_model,
                        payload,
                        content_type or "application/octet-stream",
                    )
                provider_completed = True
            finally:
                payload = None
            if not result.succeeded:
                # Surface WHY, not just that it failed. CU returns an `error`
                # object alongside the terminal status; `parse_result` already
                # keeps the whole body on `raw`, and this message used to drop
                # it — which made the failure unobservable. Discovered
                # 2026-08-07: document understanding had been enabled in
                # production and had never once enriched a document, and the
                # only evidence was `status=Failed` with no reason attached.
                #
                # Redacted because the body is remote content: it can echo file
                # names and analyzer field values back at us, and this string
                # lands in the persisted document error and in logs.
                detail = result.raw.get("error") or result.raw.get("result", {}).get("error")
                suffix = f": {redact(json.dumps(detail, default=str))[:400]}" if detail else ""
                raise RuntimeError(
                    f"content understanding status={result.status or 'unknown'}{suffix}"
                )
            analysis_pages = len(result.contents)
            # The user may have deleted the document during the (potentially long)
            # CU poll. Re-check before writing blob/vector side effects so enrich
            # never resurrects content the delete already purged.
            if not await self._still_present(user_id, document_id):
                deleted_mid_flight = True
            else:
                await self._persist_enrichment(user_id, doc, result)
                doc.analysis = DocumentAnalysis(
                    provider=provider.value,
                    model=analysis_model,
                    version=analysis_version,
                    pages=analysis_pages,
                    deployment=(
                        analysis_deployment.deploymentName
                        if analysis_deployment is not None
                        else None
                    ),
                    region=(
                        analysis_deployment.region
                        if analysis_deployment is not None
                        else None
                    ),
                    sku=(
                        analysis_deployment.sku
                        if analysis_deployment is not None
                        else None
                    ),
                    dataZone=(
                        analysis_deployment.dataZone
                        if analysis_deployment is not None
                        else None
                    ),
                    residency=(
                        analysis_deployment.residency
                        if analysis_deployment is not None
                        else None
                    ),
                )
                doc.status = DocumentStatus.ready
                doc.error = None
        except asyncio.CancelledError:
            meter_status = "cancelled"
            terminal_status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - degrade, never propagate
            meter_status = "error"
            terminal_status = "failed"
            doc.status = DocumentStatus.failed
            doc.error = str(exc)[:500]
            # _persist_enrichment may have indexed some chunk batches before the
            # failure (e.g. an embed error on a later batch). Drop the searchable
            # vectors so a failed document is never retrievable, while keeping the
            # raw upload + quick-text summary for the user to see and re-run.
            await self._purge_chunks(user_id, document_id)
            logger.warning(
                "document enrichment failed (provider=%s): %s",
                provider.value,
                exc,
                exc_info=True,
            )
        finally:
            # Always meter the selected analysis attempt.
            try:
                await self._meter_analysis(
                    user_id=user_id,
                    provider=provider,
                    model_id=analysis_model,
                    deployment=analysis_deployment,
                    pages=analysis_pages,
                    status=meter_status,
                    provider_completed=provider_completed,
                )
            except Exception:  # noqa: BLE001 - telemetry still records terminal status
                logger.warning("document analysis metering failed", exc_info=True)
            if terminal_status == "cancelled":
                pass
            elif deleted_mid_flight:
                # Honor the delete: drop any artifacts and never re-create the
                # manifest. purge is idempotent with delete_document's own purge.
                await self.purge(user_id, document_id)
                terminal_status = "cancelled"
                persistence_outcome = "missing"
            else:
                persistence_outcome, committed = await self._safe_update(
                    doc,
                    {
                        "status": doc.status,
                        "error": doc.error,
                        "summary": doc.summary,
                        "parsedPath": doc.parsedPath,
                        "chunksPath": doc.chunksPath,
                        "chunkCount": doc.chunkCount,
                        "analysis": doc.analysis,
                    },
                    require_status=DocumentStatus.analyzing,
                )
                if persistence_outcome == "missing":
                    await self.purge(user_id, document_id)
                    terminal_status = "cancelled"
                elif persistence_outcome == "error" or committed is None:
                    await self._purge_chunks(user_id, document_id)
                    terminal_status = "failed"
                else:
                    terminal_status = (
                        committed.status.value
                        if committed.status == doc.status
                        else "failed"
                    )
            emit_custom_event(
                "document_ingest_terminal",
                {
                    "status": terminal_status,
                    "modality": modality,
                    "stage": provider.value,
                    "persistenceOutcome": persistence_outcome,
                    "latencyMs": int((time.monotonic() - started) * 1000),
                },
            )

    async def _persist_enrichment(
        self, user_id: str, doc: UserDocument, result: CUResult
    ) -> None:
        markdown = result.markdown
        parsed_path = blob_path(user_id, doc.id, PARSED_NAME)
        await self._blob.put(parsed_path, markdown.encode("utf-8"), "text/markdown")
        doc.parsedPath = parsed_path

        summary = summarize_markdown(markdown)
        if summary:
            doc.summary = summary

        modality = doc.modality.value if isinstance(doc.modality, Modality) else str(doc.modality)
        # Surface the analyzer's scene/keyframe boundaries for
        # audio/video as a media.json sidecar so the web player can deep-link to
        # scenes. Independent of chunking/embedding (persisted before the early
        # return below) and best-effort — absent scene detail simply means no
        # sidecar, and the timeline endpoint degrades to an empty timeline.
        if modality in ("audio", "video"):
            timeline = media_timeline(result.contents)
            if timeline["segments"]:
                media_path = blob_path(user_id, doc.id, MEDIA_NAME)
                await self._blob.put(
                    media_path,
                    json.dumps(timeline, ensure_ascii=False).encode("utf-8"),
                    "application/json",
                )

        max_chars = self._settings.document_chunk_chars
        overlap = self._settings.document_chunk_overlap
        chunks: list = []
        if modality in ("audio", "video"):
            # Time-grounded chunking from the CU segments/transcript phrases. Falls
            # back to Markdown chunking when the analyzer returned nothing
            # groundable, so audio/video without phrase detail still indexes.
            chunks = chunk_audiovisual(result.contents, max_chars=max_chars, overlap=overlap)
        if not chunks:
            chunks = chunk_markdown(markdown, max_chars=max_chars, overlap=overlap)
        max_chunks = self._settings.document_max_chunks
        if max_chunks and len(chunks) > max_chunks:
            logger.info(
                "enrich: capping chunks %d -> %d id=%s", len(chunks), max_chunks, doc.id
            )
            chunks = chunks[:max_chunks]
        if self._chunks is not None:
            # A failed prior attempt can leave a subset of same-id chunks behind if
            # its cleanup also failed. Require a truthful purge before any retry can
            # become ready, including an empty/no-embedder retry that writes no
            # replacement chunks.
            await self._chunks.delete_document(user_id, doc.id)
        if not chunks or self._embedder is None or self._chunks is None:
            doc.chunkCount = 0
            return

        records = [
            DocChunkRecord(
                user_id=user_id,
                document_id=doc.id,
                chunk_index=c.index,
                content=c.text,
                heading=c.grounding.get("heading"),
                char_start=c.grounding.get("charStart"),
                char_end=c.grounding.get("charEnd"),
                start_ms=c.grounding.get("startMs"),
                end_ms=c.grounding.get("endMs"),
                speaker=c.grounding.get("speaker"),
            )
            for c in chunks
        ]
        # Embed + index in batches so a large document doesn't build one giant embed
        # request / vector insert.
        batch = max(1, self._settings.document_embed_batch)
        for start in range(0, len(records), batch):
            window = records[start : start + batch]
            vectors = await self._embedder.embed([r.content for r in window])
            await self._chunks.add_many(window, vectors)

        sidecar = "\n".join(json.dumps(_sidecar_row(c), ensure_ascii=False) for c in chunks)
        chunks_path = blob_path(user_id, doc.id, CHUNKS_NAME)
        await self._blob.put(chunks_path, sidecar.encode("utf-8"), "application/json")
        doc.chunksPath = chunks_path
        doc.chunkCount = len(chunks)

    async def _still_present(self, user_id: str, document_id: str) -> bool:
        """True if the manifest still exists (and is owned by ``user_id``)."""
        try:
            await self._library.get_document(user_id, document_id)
            return True
        except DocumentNotFoundError:
            return False

    async def _safe_update(
        self,
        doc: UserDocument,
        changes: dict[str, object],
        *,
        require_status: DocumentStatus | None = None,
    ) -> tuple[str, UserDocument | None]:
        """Atomically patch ingest-owned fields with bounded CAS retry."""
        try:
            updated = await self._library.patch_ingest_fields(
                doc, changes, require_status=require_status
            )
            return "committed", updated
        except DocumentNotFoundError:
            logger.info(
                "enrich: document deleted mid-flight id=%s; skipping manifest write",
                doc.id,
            )
            return "missing", None
        except Exception:  # noqa: BLE001 - terminal telemetry reports persistence failure
            logger.warning("enrich manifest update failed id=%s", doc.id, exc_info=True)
            return "error", None

    async def close(self) -> None:
        """Cancel in-flight enrich tasks, then close owned IO resources."""
        self._closed = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for resource in (self._blob, self._cu, self._chunks):
            close = getattr(resource, "close", None) if resource is not None else None
            if close is None:
                continue
            try:
                await close()
            except Exception:  # noqa: BLE001 - shutdown must not surface
                logger.warning("ingestor resource close failed", exc_info=True)

    async def recover_interrupted(self, *, now: datetime | None = None) -> int:
        """Fail out *stale* documents left mid-analysis by an interrupted worker.

        An enrich task that was cancelled on shutdown (or lost to a crash) leaves
        its manifest stuck at ``analyzing`` with no task to resume it. A second,
        healthy replica looks identical unless age is considered, so never sweep a
        fresh row: rolling deployment/scale-out can start this code while the old
        replica is still enriching it. One hour is deliberately much larger than
        the normal CU poll ceiling and indexing time. Best-effort and cross-user
        (startup only, not a hot path). Returns the number of documents swept.
        """
        try:
            stuck = await self._library.list_by_status([DocumentStatus.analyzing])
        except Exception:  # noqa: BLE001 - startup sweep must never block boot
            logger.warning("recover_interrupted: list failed", exc_info=True)
            return 0
        observed_at = now or datetime.now(timezone.utc)
        stale_seconds = max(3_600, int(self._settings.cu_max_poll_seconds) + 600)
        stale_before = observed_at - timedelta(seconds=stale_seconds)
        swept = 0
        for doc in stuck:
            updated_at = doc.updatedAt
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if updated_at > stale_before:
                continue
            outcome, committed = await self._safe_update(
                doc,
                {
                    "status": DocumentStatus.failed,
                    "parsedPath": None,
                    "chunksPath": None,
                    "chunkCount": 0,
                    "error": (
                        "Analysis was interrupted (service restart). "
                        "Re-upload to retry."
                    ),
                },
                require_status=DocumentStatus.analyzing,
            )
            if (
                outcome != "committed"
                or committed is None
                or committed.status != DocumentStatus.failed
            ):
                continue
            # Purge only the retrieval-reachable index. Preserve the raw upload
            # and quick summary so the user does not lose their file merely
            # because a replica restarted; derived blob paths are cleared above
            # and a retry overwrites their deterministic paths.
            await self._purge_chunks(doc.userId, doc.id)
            emit_custom_event(
                "document_ingest_terminal",
                {
                    "status": "failed",
                    "modality": doc.modality.value,
                    "stage": "recovery",
                    "persistenceOutcome": outcome,
                    "latencyMs": 0,
                },
            )
            swept += 1
        if swept:
            logger.info("recover_interrupted: swept %d stuck document(s)", swept)
        return swept


    async def _purge_chunks(self, user_id: str, document_id: str) -> None:
        """Drop only the document's pgvector chunk index (best-effort, idempotent).

        Unlike :meth:`purge`, the raw upload + parsed artifacts are kept — used when
        a document settles ``failed`` after a clean enrich error so the user retains
        their upload + quick-text summary, while the searchable vectors (the only
        retrieval-reachable surface) are removed.
        """
        if self._chunks is None:
            return
        try:
            await self._chunks.delete_document(user_id, document_id)
        except Exception:  # noqa: BLE001
            logger.warning("chunk purge failed id=%s", document_id, exc_info=True)

    async def purge(self, user_id: str, document_id: str) -> None:
        """Best-effort removal of a document's blob artifacts + indexed chunks.

        Called on manifest delete so storage + the vector index don't accumulate
        orphans. Never raises — deletion of the manifest is the source of truth.
        """
        from .blob_store import document_prefix

        try:
            await self._blob.delete_prefix(document_prefix(user_id, document_id))
        except Exception:  # noqa: BLE001
            logger.warning("blob purge failed id=%s", document_id, exc_info=True)
        if self._chunks is not None:
            try:
                await self._chunks.delete_document(user_id, document_id)
            except Exception:  # noqa: BLE001
                logger.warning("chunk purge failed id=%s", document_id, exc_info=True)

    async def _meter_analysis(
        self,
        *,
        user_id: str,
        provider: AnalyzerProvider,
        model_id: str,
        deployment: DeploymentOption | None,
        pages: int | None,
        status: str,
        provider_completed: bool,
    ) -> None:
        # Content Understanding has no catalog deployment; Mistral does.
        if deployment is None:
            deployment = DeploymentOption(
                region="unknown",
                sku="content-understanding",
                deploymentName=model_id,
            )
        await self._usage.record_completion(
            user_id=user_id,
            session_id=_INGEST_SESSION,
            model_id=(
                _CU_MODEL_ID
                if provider is AnalyzerProvider.content_understanding
                else model_id
            ),
            target=UsageTarget.from_deployment(
                deployment, provider=provider.value
            ),
            usage=TokenUsage(known=False, complete=False, calls=1),
            status="complete" if status == "complete" else "error",
            provider_completed=provider_completed,
            billable_units=(
                pages
                if provider is AnalyzerProvider.mistral and pages is not None
                else None
            ),
            billing_unit=(
                "page"
                if provider is AnalyzerProvider.mistral and pages is not None
                else None
            ),
        )
