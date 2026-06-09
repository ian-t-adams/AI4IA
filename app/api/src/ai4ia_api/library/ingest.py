"""Document ingest orchestrator (Phase 11B producer path).

Turns an upload into a manifest + retrievable chunks, governed and fail-soft:

1. :meth:`ingest` (sync, on the request path) — hash + content-dedupe, persist the
   raw bytes to blob, derive an instant quick-text summary (Phase 7C extractor,
   best-effort), and create the manifest at status ``stored``. Returns immediately
   so the upload feels fast; if the same bytes+analyzer were ingested before it
   returns the existing manifest (no re-crack).
2. :meth:`enrich` (async, scheduled as a background task) — run Content
   Understanding, persist ``parsed.md``, chunk + embed into the per-user
   ``doc_chunks`` vector store, write a ``chunks.jsonl`` sidecar, then flip the
   manifest to ``ready`` (or ``failed`` with the quick-text fallback retained).

Governance: one CU operation is metered per enrich attempt via a *synthetic*
``DeploymentOption`` (CU has no catalog entry); usage is ``known=False`` so it is
never priced, mirroring the voice/realtime "unknown call" convention. Embeddings
are not separately metered (consistent with memory). ``enrich`` never raises — it
runs detached from the response — so a CU/storage failure degrades the document to
``failed`` rather than surfacing anywhere.

All IO is injected (blob store, CU client, embedder, chunk store), so the
orchestrator is unit-tested end to end without network or Azure SDKs.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from ..catalog import DeploymentOption
from ..config import Settings
from ..documents.extract import DocumentError, extract_text
from ..memory.embedder import GatewayEmbedder
from ..usage.models import TokenUsage
from ..usage.service import UsageService
from .blob_store import CHUNKS_NAME, PARSED_NAME, RAW_NAME, BlobStore, blob_path
from .chunking import chunk_markdown
from .doc_chunks import DocChunkRecord, DocChunkStore
from .hashing import content_hash
from .modality import classify_modality
from .models import (
    BUILTIN_ANALYZER_IDS,
    Analyzer,
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


@dataclass(slots=True)
class IngestResult:
    document: UserDocument
    # True when an identical (bytes, analyzer) upload already existed — no work
    # was scheduled, the existing manifest is returned as-is.
    deduped: bool


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
        embedder: GatewayEmbedder | None = None,
        chunk_store: DocChunkStore | None = None,
    ) -> None:
        self._library = library
        self._blob = blob_store
        self._settings = settings
        self._usage = usage
        self._cu = cu_client
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
        data: bytes,
        content_type: str,
    ) -> None:
        """Fire-and-forget :meth:`enrich`, tracked so delete/shutdown can cancel.

        A no-op when CU is not configured (the document simply settles at
        ``stored``) or after :meth:`close`. Replaces an untracked
        ``BackgroundTasks.add_task`` so an in-flight crack can be cancelled the
        moment the user deletes the document.
        """
        if self._cu is None or self._closed:
            return
        key = (user_id, document_id)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self.enrich(
                user_id=user_id,
                document_id=document_id,
                data=data,
                content_type=content_type,
            )
        )
        self._tasks[key] = task

        def _done(t: asyncio.Task[None], _key=key) -> None:
            if self._tasks.get(_key) is t:
                self._tasks.pop(_key, None)
            if not t.cancelled():
                # enrich() never raises, but retrieve any exception so it is not
                # reported as "never retrieved".
                exc = t.exception()
                if exc is not None:  # pragma: no cover - defensive
                    logger.warning("enrich task errored: %s", exc, exc_info=exc)

        task.add_done_callback(_done)

    async def cancel_enrich(self, user_id: str, document_id: str) -> None:
        """Cancel and drain the in-flight enrich for a document, if any."""
        task = self._tasks.pop((user_id, document_id), None)
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

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
        if not analyzer_id or analyzer_id in BUILTIN_ANALYZER_IDS:
            return None
        try:
            return await self._library.get_analyzer(user_id, analyzer_id)
        except AnalyzerNotFoundError:
            return None

    async def enrich(
        self,
        *,
        user_id: str,
        document_id: str,
        data: bytes,
        content_type: str,
    ) -> None:
        """Crack with CU, index chunks, and flip the manifest to ready/failed.

        Scheduled as a background task; never raises. A no-op (leaves the document
        at ``stored``) when CU is not configured.
        """
        if self._cu is None:
            return
        try:
            doc = await self._library.get_document(user_id, document_id)
        except DocumentNotFoundError:
            logger.warning("enrich: document gone user=%s id=%s", user_id, document_id)
            return

        modality = doc.modality.value if isinstance(doc.modality, Modality) else str(doc.modality)
        analyzer = await self._resolve_analyzer(user_id, doc.analyzerId)
        cu_analyzer_id = resolve_cu_analyzer_id(analyzer, modality, self._settings)

        doc.status = DocumentStatus.analyzing
        doc.touch()
        if not await self._safe_update(doc):
            # Deleted in the (small) window before analysis began; nothing was
            # persisted beyond the raw upload, which delete_document already purged.
            return

        meter_status = "complete"
        deleted_mid_flight = False
        try:
            result = await self._cu.analyze(
                cu_analyzer_id, data, content_type or "application/octet-stream"
            )
            if not result.succeeded:
                raise RuntimeError(
                    f"content understanding status={result.status or 'unknown'}"
                )
            # The user may have deleted the document during the (potentially long)
            # CU poll. Re-check before writing blob/vector side effects so enrich
            # never resurrects content the delete already purged.
            if not await self._still_present(user_id, document_id):
                deleted_mid_flight = True
            else:
                await self._persist_enrichment(user_id, doc, result.markdown)
                doc.status = DocumentStatus.ready
                doc.error = None
        except Exception as exc:  # noqa: BLE001 - degrade, never propagate
            meter_status = "error"
            doc.status = DocumentStatus.failed
            doc.error = str(exc)[:500]
            # _persist_enrichment may have indexed some chunk batches before the
            # failure (e.g. an embed error on a later batch). Drop the searchable
            # vectors so a failed document is never retrievable, while keeping the
            # raw upload + quick-text summary for the user to see and re-run.
            await self._purge_chunks(user_id, document_id)
            logger.warning(
                "enrich failed user=%s id=%s: %s", user_id, document_id, exc, exc_info=True
            )
        finally:
            # Always meter the CU attempt (one synthetic op per enrich).
            await self._meter_cu(user_id, cu_analyzer_id, meter_status)
            if deleted_mid_flight:
                # Honor the delete: drop any artifacts and never re-create the
                # manifest. purge is idempotent with delete_document's own purge.
                await self.purge(user_id, document_id)
                return
            doc.touch()
            # The terminal manifest write is the commit point. If the document was
            # deleted between the re-check and here, update_document raises
            # DocumentNotFoundError; we then roll back the just-written artifacts so
            # the delete wins deterministically (no orphaned blob/vector chunks).
            if not await self._safe_update(doc):
                await self.purge(user_id, document_id)

    async def _persist_enrichment(
        self, user_id: str, doc: UserDocument, markdown: str
    ) -> None:
        parsed_path = blob_path(user_id, doc.id, PARSED_NAME)
        await self._blob.put(parsed_path, markdown.encode("utf-8"), "text/markdown")
        doc.parsedPath = parsed_path

        summary = summarize_markdown(markdown)
        if summary:
            doc.summary = summary

        chunks = chunk_markdown(
            markdown,
            max_chars=self._settings.document_chunk_chars,
            overlap=self._settings.document_chunk_overlap,
        )
        max_chunks = self._settings.document_max_chunks
        if max_chunks and len(chunks) > max_chunks:
            logger.info(
                "enrich: capping chunks %d -> %d id=%s", len(chunks), max_chunks, doc.id
            )
            chunks = chunks[:max_chunks]
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

        sidecar = "\n".join(
            json.dumps(
                {
                    "index": c.index,
                    "text": c.text,
                    "heading": c.grounding.get("heading"),
                    "charStart": c.grounding.get("charStart"),
                    "charEnd": c.grounding.get("charEnd"),
                },
                ensure_ascii=False,
            )
            for c in chunks
        )
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

    async def _safe_update(self, doc: UserDocument) -> bool:
        """Persist the manifest during enrich.

        Returns ``False`` only when the document was deleted mid-flight
        (``update_document`` raised ``DocumentNotFoundError``) — the caller then
        rolls back any artifacts so the delete wins. A transient error is logged
        and returns ``True`` (best-effort; not a deletion, so no rollback).
        """
        try:
            await self._library.update_document(doc)
            return True
        except DocumentNotFoundError:
            logger.info(
                "enrich: document deleted mid-flight id=%s; skipping manifest write",
                doc.id,
            )
            return False
        except Exception:  # noqa: BLE001 - manifest write is best-effort in enrich
            logger.warning("enrich manifest update failed id=%s", doc.id, exc_info=True)
            return True

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

    async def recover_interrupted(self) -> int:
        """Fail out documents left mid-analysis by an interrupted worker.

        An enrich task that was cancelled on shutdown (or lost to a crash) leaves
        its manifest stuck at ``analyzing`` with no task to resume it. Run once at
        startup: flip every such document to ``failed`` with a recoverable message
        so it is not a permanent zombie. Best-effort and cross-user (startup only,
        not a hot path). Returns the number of documents swept.
        """
        try:
            stuck = await self._library.list_by_status([DocumentStatus.analyzing])
        except Exception:  # noqa: BLE001 - startup sweep must never block boot
            logger.warning("recover_interrupted: list failed", exc_info=True)
            return 0
        swept = 0
        for doc in stuck:
            doc.status = DocumentStatus.failed
            doc.error = "Analysis was interrupted (service restart). Re-upload to retry."
            doc.touch()
            try:
                await self._library.update_document(doc)
            except DocumentNotFoundError:
                continue
            except Exception:  # noqa: BLE001
                logger.warning(
                    "recover_interrupted: update failed id=%s", doc.id, exc_info=True
                )
                continue
            # An enrich cancelled inside _persist_enrichment (shutdown/crash) may
            # have written partial blob/pgvector artifacts before dying, with the
            # manifest left at ``analyzing``. Now that it is ``failed``, purge those
            # so a failed document contributes nothing to retrieval (no orphan
            # chunks under a failed manifest). Best-effort + idempotent.
            await self.purge(doc.userId, doc.id)
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

    async def _meter_cu(self, user_id: str, cu_analyzer_id: str, status: str) -> None:
        # CU has no catalog deployment; meter against a synthetic one. Usage is
        # known=False so it is counted but never priced (no catalog/price lookup).
        deployment = DeploymentOption(
            region="unknown",
            sku="content-understanding",
            deploymentName=cu_analyzer_id,
        )
        await self._usage.record_completion(
            user_id=user_id,
            session_id=_INGEST_SESSION,
            model_id=_CU_MODEL_ID,
            deployment=deployment,
            usage=TokenUsage(known=False, complete=False, calls=1),
            status="complete" if status == "complete" else "error",
        )
