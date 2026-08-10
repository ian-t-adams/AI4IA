"""Document ingest orchestrator: dedupe, status transitions, the
CU-success enrichment (parsed.md + chunks + metering), CU-failure degrade, and
the CU-disabled no-op. All IO is injected (in-memory stores + fakes)."""
from __future__ import annotations

import pytest

from ai4ia_api.content_understanding.models import CUResult
from ai4ia_api.library.blob_store import (
    InMemoryBlobStore,
    blob_path,
    document_prefix,
)
from ai4ia_api.library.doc_chunks import DocChunkRecord, InMemoryDocChunkStore
from ai4ia_api.library.ingest import DocumentIngestor, resolve_cu_analyzer_id
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.library.models import (
    Analyzer,
    AnalyzerKind,
    DocumentStatus,
    Modality,
    UserDocument,
)
from ai4ia_api.library.repository import DocumentNotFoundError
from tests.conftest import make_settings


class FakeUsage:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def record_completion(self, **kwargs) -> None:
        self.calls.append(kwargs)


class FakeEmbedder:
    def __init__(self, dim: int = 3) -> None:
        self.dim = dim
        self.embedded: list[str] = []

    async def embed(self, inputs):
        self.embedded.extend(inputs)
        return [[float(len(t) % 5), 1.0, 0.0] for t in inputs]


class FakeCU:
    def __init__(self, result: CUResult | None = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple] = []

    async def analyze(self, analyzer_id, data, content_type):
        self.calls.append((analyzer_id, data, content_type))
        if self._error is not None:
            raise self._error
        return self._result


def _succeeded(markdown: str) -> CUResult:
    return CUResult(status="Succeeded", analyzer_id="prebuilt-documentSearch", markdown=markdown)


def _make(*, cu=None, embedder=None, chunks=None, usage=None, library=None, **settings_overrides):
    settings = make_settings(
        document_understanding_enabled=True,
        document_chunk_chars=40,
        document_chunk_overlap=5,
        **settings_overrides,
    )
    return DocumentIngestor(
        library=library or InMemoryDocumentLibraryRepository(),
        blob_store=InMemoryBlobStore(),
        settings=settings,
        usage=usage or FakeUsage(),
        cu_client=cu,
        embedder=embedder,
        chunk_store=chunks,
    )


# --- resolve_cu_analyzer_id ---
def test_resolver_defaults_to_modality_analyzer():
    settings = make_settings(document_understanding_enabled=True)
    assert resolve_cu_analyzer_id(None, "image", settings) == settings.cu_image_analyzer


def test_resolver_uses_custom_base_analyzer():
    settings = make_settings(document_understanding_enabled=True)
    custom = Analyzer(
        userId="u1", name="Invoices", kind=AnalyzerKind.custom, baseAnalyzerId="prebuilt-invoice"
    )
    assert resolve_cu_analyzer_id(custom, "document", settings) == "prebuilt-invoice"


# --- ingest (sync) ---
async def test_ingest_creates_manifest_and_stores_bytes():
    ingestor = _make()
    result = await ingestor.ingest(
        user_id="u1", filename="report.pdf", content_type="application/pdf", data=b"PDFDATA"
    )
    doc = result.document
    assert not result.deduped
    assert doc.status == DocumentStatus.stored
    assert doc.modality == Modality.document
    assert doc.rawPath == blob_path("u1", doc.id, "original.pdf")
    assert doc.contentHash


async def test_ingest_quick_text_summary_for_text_upload():
    ingestor = _make()
    result = await ingestor.ingest(
        user_id="u1", filename="note.txt", content_type="text/plain", data=b"hello there world"
    )
    assert result.document.summary == "hello there world"


async def test_ingest_is_idempotent_on_dedupe_key():
    library = InMemoryDocumentLibraryRepository()
    ingestor = _make(library=library)
    first = await ingestor.ingest(
        user_id="u1", filename="a.pdf", content_type="application/pdf", data=b"SAME"
    )
    second = await ingestor.ingest(
        user_id="u1", filename="a.pdf", content_type="application/pdf", data=b"SAME"
    )
    assert second.deduped
    assert second.document.id == first.document.id
    assert len(await library.list_documents("u1")) == 1


# --- enrich (background) ---
async def test_enrich_success_indexes_chunks_and_meters(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.library.ingest.emit_custom_event",
        lambda name, attributes: events.append((name, attributes)),
    )
    library = InMemoryDocumentLibraryRepository()
    usage = FakeUsage()
    embedder = FakeEmbedder()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    cu = FakeCU(result=_succeeded("# Title\n\n" + ("alpha beta " * 10)))
    ingestor = _make(cu=cu, embedder=embedder, chunks=chunks, usage=usage, library=library)

    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"BYTES"
    )
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"BYTES", content_type="application/pdf"
    )

    doc = await library.get_document("u1", stored.document.id)
    assert doc.status == DocumentStatus.ready
    assert doc.parsedPath is not None
    assert doc.chunksPath is not None
    assert doc.chunkCount > 0
    # chunks landed in the vector store and were embedded
    hits = await chunks.search("u1", [1.0, 1.0, 0.0], top_k=50)
    assert len(hits) == doc.chunkCount
    assert embedder.embedded
    # exactly one metered CU op, status complete, unknown usage
    assert len(usage.calls) == 1
    call = usage.calls[0]
    assert call["status"] == "complete"
    assert call["session_id"] == "document-ingest"
    assert call["model_id"] == "content-understanding"
    assert call["usage"].known is False and call["usage"].calls == 1
    assert events[-1][0] == "document_ingest_terminal"
    assert events[-1][1]["status"] == "ready"
    assert set(events[-1][1]) == {
        "status",
        "modality",
        "stage",
        "persistenceOutcome",
        "latencyMs",
    }


async def test_enrich_cu_failure_degrades_to_failed_and_meters_error(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.library.ingest.emit_custom_event",
        lambda name, attributes: events.append((name, attributes)),
    )
    library = InMemoryDocumentLibraryRepository()
    usage = FakeUsage()
    cu = FakeCU(error=RuntimeError("upstream boom"))
    ingestor = _make(cu=cu, usage=usage, library=library)

    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"BYTES"
    )
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"BYTES", content_type="application/pdf"
    )

    doc = await library.get_document("u1", stored.document.id)
    assert doc.status == DocumentStatus.failed
    assert doc.error
    assert usage.calls[0]["status"] == "error"
    assert events[-1][0] == "document_ingest_terminal"
    assert events[-1][1]["status"] == "failed"


async def test_enrich_partial_chunk_write_fails_and_purges_searchable_chunks():
    class PartiallyFailingChunkStore(InMemoryDocChunkStore):
        async def add_many(self, records, vectors):
            await super().add_many(records[:1], vectors[:1])
            raise RuntimeError("partial indexing failure")

    library = InMemoryDocumentLibraryRepository()
    usage = FakeUsage()
    chunks = PartiallyFailingChunkStore(expected_dim=3)
    ingestor = _make(
        cu=FakeCU(result=_succeeded("# T\n\n" + ("word " * 30))),
        embedder=FakeEmbedder(),
        chunks=chunks,
        usage=usage,
        library=library,
    )
    stored = await ingestor.ingest(
        user_id="u1",
        filename="d.pdf",
        content_type="application/pdf",
        data=b"X",
    )

    await ingestor.enrich(
        user_id="u1",
        document_id=stored.document.id,
        data=b"X",
        content_type="application/pdf",
    )

    doc = await library.get_document("u1", stored.document.id)
    assert doc.status == DocumentStatus.failed
    assert "partial indexing failure" in (doc.error or "")
    assert usage.calls[0]["status"] == "error"
    assert await chunks.search("u1", [1.0, 1.0, 0.0], top_k=50) == []


async def test_enrich_never_reindexes_when_stale_chunk_cleanup_fails():
    class CleanupFailingChunkStore(InMemoryDocChunkStore):
        def __init__(self) -> None:
            super().__init__(expected_dim=3)
            self.add_calls = 0

        async def delete_document(self, user_id, document_id):
            raise RuntimeError("stale chunk cleanup failed")

        async def add_many(self, records, vectors):
            self.add_calls += 1
            await super().add_many(records, vectors)

    library = InMemoryDocumentLibraryRepository()
    chunks = CleanupFailingChunkStore()
    ingestor = _make(
        cu=FakeCU(result=_succeeded("# T\n\n" + ("word " * 30))),
        embedder=FakeEmbedder(),
        chunks=chunks,
        library=library,
    )
    stored = await ingestor.ingest(
        user_id="u1",
        filename="d.pdf",
        content_type="application/pdf",
        data=b"X",
    )

    await ingestor.enrich(
        user_id="u1",
        document_id=stored.document.id,
        data=b"X",
        content_type="application/pdf",
    )

    doc = await library.get_document("u1", stored.document.id)
    assert doc.status == DocumentStatus.failed
    assert "stale chunk cleanup failed" in (doc.error or "")
    assert chunks.add_calls == 0


async def test_enrich_empty_result_purges_stale_chunks_before_ready():
    library = InMemoryDocumentLibraryRepository()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    ingestor = _make(
        cu=FakeCU(result=_succeeded("")),
        embedder=FakeEmbedder(),
        chunks=chunks,
        library=library,
    )
    stored = await ingestor.ingest(
        user_id="u1",
        filename="d.pdf",
        content_type="application/pdf",
        data=b"X",
    )
    stale = DocChunkRecord(
        user_id="u1",
        document_id=stored.document.id,
        chunk_index=0,
        content="STALE CONTENT",
    )
    await chunks.add_many([stale], [[1.0, 0.0, 0.0]])

    await ingestor.enrich(
        user_id="u1",
        document_id=stored.document.id,
        data=b"X",
        content_type="application/pdf",
    )

    doc = await library.get_document("u1", stored.document.id)
    assert doc.status == DocumentStatus.ready
    assert doc.chunkCount == 0
    assert await chunks.search("u1", [1.0, 0.0, 0.0], top_k=10) == []


async def test_enrich_failed_status_result_degrades():
    library = InMemoryDocumentLibraryRepository()
    cu = FakeCU(result=CUResult(status="Failed", analyzer_id="a", markdown=""))
    ingestor = _make(cu=cu, library=library)
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"X"
    )
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"X", content_type="application/pdf"
    )
    doc = await library.get_document("u1", stored.document.id)
    assert doc.status == DocumentStatus.failed


async def test_terminal_manifest_failure_never_emits_ready(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.library.ingest.emit_custom_event",
        lambda name, attributes: events.append((name, attributes)),
    )

    class FailingFinalRepository(InMemoryDocumentLibraryRepository):
        def __init__(self) -> None:
            super().__init__()
            self.patches = 0

        async def patch_ingest_fields(self, document, changes, **kwargs):
            self.patches += 1
            if self.patches == 2:
                raise RuntimeError("manifest unavailable")
            return await super().patch_ingest_fields(document, changes, **kwargs)

    library = FailingFinalRepository()
    ingestor = _make(
        cu=FakeCU(result=_succeeded("# ready")),
        embedder=FakeEmbedder(),
        chunks=InMemoryDocChunkStore(expected_dim=3),
        library=library,
    )
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"X"
    )
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"X", content_type="application/pdf"
    )
    final = await library.get_document("u1", stored.document.id)
    assert final.status == DocumentStatus.analyzing
    terminal = [
        attributes
        for name, attributes in events
        if name == "document_ingest_terminal"
    ][-1]
    assert terminal["status"] == "failed"
    assert terminal["persistenceOutcome"] == "error"


async def test_enrich_is_noop_when_cu_disabled():
    library = InMemoryDocumentLibraryRepository()
    usage = FakeUsage()
    ingestor = _make(cu=None, usage=usage, library=library)
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"X"
    )
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"X", content_type="application/pdf"
    )
    doc = await library.get_document("u1", stored.document.id)
    # Untouched: stays at stored, nothing metered.
    assert doc.status == DocumentStatus.stored
    assert usage.calls == []


async def test_purge_removes_blob_and_chunks():
    library = InMemoryDocumentLibraryRepository()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    embedder = FakeEmbedder()
    cu = FakeCU(result=_succeeded("# T\n\n" + ("word " * 20)))
    ingestor = _make(cu=cu, embedder=embedder, chunks=chunks, library=library)
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"B"
    )
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"B", content_type="application/pdf"
    )
    assert (await chunks.search("u1", [1.0, 1.0, 0.0], top_k=50))

    await ingestor.purge("u1", stored.document.id)
    assert (await chunks.search("u1", [1.0, 1.0, 0.0], top_k=50)) == []


# --- enrich vs. delete (resurrection regression) ---
def _blob_keys(ingestor: DocumentIngestor, user_id: str, document_id: str) -> list[str]:
    prefix = document_prefix(user_id, document_id)
    return [k for k in ingestor._blob._data if k.startswith(prefix)]


async def test_enrich_deleted_during_cu_poll_does_not_resurrect():
    """A delete that lands during the CU poll must not be resurrected by enrich:
    no manifest, no orphaned vector chunks, no orphaned blobs."""
    library = InMemoryDocumentLibraryRepository()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    embedder = FakeEmbedder()
    ingestor = _make(embedder=embedder, chunks=chunks, library=library)

    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"B"
    )
    doc_id = stored.document.id

    class DeletingCU:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def analyze(self, analyzer_id, data, content_type):
            self.calls.append((analyzer_id, data, content_type))
            # User deletes mid-poll — mirror the router: manifest delete + purge.
            await library.delete_document("u1", doc_id)
            await ingestor.purge("u1", doc_id)
            return _succeeded("# T\n\n" + ("word " * 30))

    ingestor._cu = DeletingCU()
    await ingestor.enrich(
        user_id="u1", document_id=doc_id, data=b"B", content_type="application/pdf"
    )

    with pytest.raises(DocumentNotFoundError):
        await library.get_document("u1", doc_id)
    assert (await chunks.search("u1", [1.0, 1.0, 0.0], top_k=50)) == []
    assert _blob_keys(ingestor, "u1", doc_id) == []


async def test_enrich_delete_between_recheck_and_commit_rolls_back():
    """If the delete lands after the existence re-check but before the terminal
    manifest write (the commit point), the freshly-written artifacts are rolled
    back so the delete wins deterministically."""

    class _DeleteOnReadyRepo(InMemoryDocumentLibraryRepository):
        def __init__(self) -> None:
            super().__init__()
            self._tripped = False

        async def patch_ingest_fields(self, document, changes, **kwargs):
            # The terminal success write is the commit point; simulate the row
            # having just been deleted so the write loses to the concurrent delete.
            if changes.get("status") == DocumentStatus.ready and not self._tripped:
                self._tripped = True
                await super().delete_document(document.userId, document.id)
                raise DocumentNotFoundError(document.id)
            return await super().patch_ingest_fields(document, changes, **kwargs)

    library = _DeleteOnReadyRepo()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    embedder = FakeEmbedder()
    cu = FakeCU(result=_succeeded("# T\n\n" + ("word " * 30)))
    ingestor = _make(cu=cu, embedder=embedder, chunks=chunks, library=library)

    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"B"
    )
    doc_id = stored.document.id
    await ingestor.enrich(
        user_id="u1", document_id=doc_id, data=b"B", content_type="application/pdf"
    )

    with pytest.raises(DocumentNotFoundError):
        await library.get_document("u1", doc_id)
    # Persist ran (chunks/blobs were written) but the commit-point loss rolled
    # them back — nothing survives the delete.
    assert (await chunks.search("u1", [1.0, 1.0, 0.0], top_k=50)) == []
    assert _blob_keys(ingestor, "u1", doc_id) == []


async def test_update_document_missing_raises_in_memory_repo():
    """The in-memory repo (the contract Cosmos must match) raises rather than
    silently creating when updating a document id that does not exist."""
    library = InMemoryDocumentLibraryRepository()
    ghost = UserDocument(
        userId="u1",
        filename="gone.pdf",
        contentType="application/pdf",
        size=1,
        contentHash="deadbeef",
        modality=Modality.document,
        status=DocumentStatus.ready,
    )
    with pytest.raises(DocumentNotFoundError):
        await library.update_document(ghost)


# --- chunk cap + batched embed (resource bound) ---
async def test_enrich_caps_chunks_and_batches_embed():
    library = InMemoryDocumentLibraryRepository()
    chunks = InMemoryDocChunkStore(expected_dim=3)

    class CountingEmbedder(FakeEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.batches = 0

        async def embed(self, inputs):
            self.batches += 1
            return await super().embed(inputs)

    embedder = CountingEmbedder()
    # ~28-char paragraphs at document_chunk_chars=40 → one chunk each (>3 total).
    md = "# T\n\n" + "\n\n".join(f"paragraph number {i:02d} text" for i in range(12))
    cu = FakeCU(result=_succeeded(md))
    ingestor = _make(
        cu=cu,
        embedder=embedder,
        chunks=chunks,
        library=library,
        document_max_chunks=3,
        document_embed_batch=1,
    )

    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"B"
    )
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"B", content_type="application/pdf"
    )

    doc = await library.get_document("u1", stored.document.id)
    assert doc.status == DocumentStatus.ready
    assert doc.chunkCount == 3  # capped from >3
    hits = await chunks.search("u1", [1.0, 1.0, 0.0], top_k=50)
    assert len(hits) == 3
    assert embedder.batches == 3  # batch size 1 → one embed round-trip per chunk


# --- audio/video time-grounded enrich ---
def _succeeded_av(contents: list[dict], markdown: str = "WEBVTT transcript") -> CUResult:
    return CUResult(
        status="Succeeded",
        analyzer_id="prebuilt-audioSearch",
        markdown=markdown,
        contents=contents,
    )


async def test_enrich_audio_indexes_time_grounded_chunks():
    library = InMemoryDocumentLibraryRepository()
    embedder = FakeEmbedder()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    contents = [
        {
            "kind": "audioVisual",
            "startTimeMs": 0,
            "endTimeMs": 5000,
            "transcriptPhrases": [
                {"text": "Hello.", "startTimeMs": 1000, "endTimeMs": 2000, "speaker": "Speaker 1"},
                {"text": "Welcome.", "startTimeMs": 2000, "endTimeMs": 4000, "speaker": "Speaker 1"},
            ],
        }
    ]
    cu = FakeCU(result=_succeeded_av(contents))
    ingestor = _make(cu=cu, embedder=embedder, chunks=chunks, library=library)

    stored = await ingestor.ingest(
        user_id="u1", filename="lecture.mp3", content_type="audio/mpeg", data=b"AUDIO"
    )
    assert stored.document.modality == Modality.audio
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"AUDIO", content_type="audio/mpeg"
    )

    doc = await library.get_document("u1", stored.document.id)
    assert doc.status == DocumentStatus.ready
    assert doc.chunkCount > 0
    hits = await chunks.search("u1", [1.0, 1.0, 0.0], top_k=50)
    grounded = [h for h in hits if h.start_ms is not None]
    assert grounded, "expected at least one time-grounded chunk"
    assert grounded[0].speaker == "Speaker 1"
    assert grounded[0].start_ms == 1000


async def test_enrich_audio_without_phrases_falls_back_to_markdown():
    library = InMemoryDocumentLibraryRepository()
    embedder = FakeEmbedder()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    # No transcriptPhrases and no per-segment markdown → fall back to the
    # concatenated result markdown so the document still indexes + goes ready.
    cu = FakeCU(result=_succeeded_av([], markdown="# Talk\n\nfull transcript text body"))
    ingestor = _make(cu=cu, embedder=embedder, chunks=chunks, library=library)

    stored = await ingestor.ingest(
        user_id="u1", filename="clip.mp4", content_type="video/mp4", data=b"VIDEO"
    )
    assert stored.document.modality == Modality.video
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"VIDEO", content_type="video/mp4"
    )

    doc = await library.get_document("u1", stored.document.id)
    assert doc.status == DocumentStatus.ready
    assert doc.chunkCount > 0
    hits = await chunks.search("u1", [1.0, 1.0, 0.0], top_k=50)
    # Markdown fallback → no time grounding, but content is retrievable.
    assert all(h.start_ms is None for h in hits)
    assert hits




@pytest.mark.asyncio
async def test_cu_failure_records_the_reason_not_just_the_status():
    """A terminal CU failure must say WHY, or the feature is unobservable.

    Not hypothetical tidying. Document understanding was enabled in production
    and had **never once** enriched a document; the only evidence anywhere was
    ``content understanding status=Failed`` with the reason discarded, so
    nothing could be diagnosed without first adding logging and waiting for a
    redeploy. CU returns an ``error`` object beside the terminal status and
    ``parse_result`` already keeps the whole body on ``raw`` -- the reason was
    being thrown away one line before it was needed.
    """
    library = InMemoryDocumentLibraryRepository()
    failed = CUResult(
        status="Failed",
        analyzer_id="prebuilt-documentSearch",
        markdown="",
        raw={
            "status": "Failed",
            "error": {"code": "InvalidArgument", "message": "Analyzer not found."},
        },
    )
    ingestor = _make(cu=FakeCU(result=failed), library=library)
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"BYTES"
    )
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"BYTES", content_type="application/pdf"
    )

    doc = await library.get_document("u1", stored.document.id)
    assert doc is not None
    assert doc.status is DocumentStatus.failed
    # The bare status is what shipped before, and it proved nothing.
    assert "InvalidArgument" in (doc.error or ""), doc.error
    assert "Analyzer not found." in (doc.error or ""), doc.error


@pytest.mark.asyncio
async def test_cu_failure_without_an_error_object_still_reports_the_status():
    """Control: the reason is additive, never a new failure mode.

    If CU returns a terminal failure with no ``error`` payload, enrich must
    degrade exactly as it always did rather than raising on a missing key.
    Without this, the test above could be satisfied by code that only works
    when an error object happens to be present.
    """
    library = InMemoryDocumentLibraryRepository()
    failed = CUResult(
        status="Failed",
        analyzer_id="prebuilt-documentSearch",
        markdown="",
        raw={"status": "Failed"},
    )
    ingestor = _make(cu=FakeCU(result=failed), library=library)
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"BYTES"
    )
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"BYTES", content_type="application/pdf"
    )

    doc = await library.get_document("u1", stored.document.id)
    assert doc is not None
    assert doc.status is DocumentStatus.failed
    assert "status=Failed" in (doc.error or ""), doc.error
    # Tightened after mutation testing: asserting only that the status is
    # present let a mutant through that appended the reason unconditionally.
    # `json.dumps(None)` is the string "null", so the bare-status assertion
    # stayed true while the message gained a meaningless `: null` suffix. The
    # message must end AT the status when there is nothing to add.
    assert (doc.error or "").endswith("status=Failed"), doc.error
    assert "null" not in (doc.error or ""), doc.error


@pytest.mark.asyncio
async def test_cu_failure_reason_is_redacted_before_it_is_persisted():
    """The CU error body is REMOTE content and lands in a persisted field.

    It can echo file names, analyzer field values, or an upstream URL carrying a
    token back at us, and this string is written to the document row and to
    logs. Added because mutation testing showed the two tests above passed
    unchanged with `redact()` removed -- their fixtures contained nothing
    secret-shaped, so they could not tell redaction from no redaction.
    """
    library = InMemoryDocumentLibraryRepository()
    failed = CUResult(
        status="Failed",
        analyzer_id="prebuilt-documentSearch",
        markdown="",
        raw={
            "status": "Failed",
            "error": {
                "code": "InvalidArgument",
                # Deliberately shaped like a credential without being one:
                # `redact()` keys off the `api_key=` prefix, so the value only
                # needs to look opaque. A realistic-looking token here trips
                # gitleaks in CI (it scans history and matches on shape, not on
                # whether the string is real), and an allowlist entry to carry a
                # fake secret would be worse than a duller fixture.
                "message": "callback failed for api_key=" + ("Z" * 24),
            },
        },
    )
    ingestor = _make(cu=FakeCU(result=failed), library=library)
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"BYTES"
    )
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"BYTES", content_type="application/pdf"
    )

    doc = await library.get_document("u1", stored.document.id)
    assert doc is not None
    error = doc.error or ""
    # Non-vacuity: the reason really did make it through...
    assert "InvalidArgument" in error, error
    # ...and the credential-shaped value in it did not.
    assert ("Z" * 24) not in error, error
