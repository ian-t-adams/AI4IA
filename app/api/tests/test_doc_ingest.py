"""Document ingest orchestrator (Phase 11B): dedupe, status transitions, the
CU-success enrichment (parsed.md + chunks + metering), CU-failure degrade, and
the CU-disabled no-op. All IO is injected (in-memory stores + fakes)."""
from __future__ import annotations

from ai4ia_api.content_understanding.models import CUResult
from ai4ia_api.library.blob_store import InMemoryBlobStore, blob_path
from ai4ia_api.library.doc_chunks import InMemoryDocChunkStore
from ai4ia_api.library.ingest import DocumentIngestor, resolve_cu_analyzer_id
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.library.models import (
    Analyzer,
    AnalyzerKind,
    DocumentStatus,
    Modality,
)
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


def _make(*, cu=None, embedder=None, chunks=None, usage=None, library=None):
    settings = make_settings(
        document_understanding_enabled=True,
        document_chunk_chars=40,
        document_chunk_overlap=5,
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
async def test_enrich_success_indexes_chunks_and_meters():
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


async def test_enrich_cu_failure_degrades_to_failed_and_meters_error():
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
