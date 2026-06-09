"""Document retrieval consumer (Phase 11B-2): Tier 1 summary cards, Tier 2 RAG
excerpts, and the Tier 3 fetch_document capability — all status-gated to ``ready``
documents, nonce-fenced, and best-effort. All IO is injected (in-memory stores +
a fake embedder); no network."""
from __future__ import annotations

from ai4ia_api.library.blob_store import PARSED_NAME, InMemoryBlobStore, blob_path
from ai4ia_api.library.chat_capability import (
    MAX_FETCHES_PER_TURN,
    build_document_capability,
)
from ai4ia_api.library.doc_chunks import DocChunkRecord, InMemoryDocChunkStore
from ai4ia_api.library.ingest import DocumentIngestor
from ai4ia_api.library.ingest_factory import build_document_retrieval
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.library.models import DocumentStatus, UserDocument
from ai4ia_api.library.retrieval import DocumentRetrievalService
from tests.conftest import make_settings


class FakeEmbedder:
    def __init__(self, vector=None) -> None:
        self._vector = list(vector or [1.0, 0.0, 0.0])
        self.queries: list[str] = []

    async def embed(self, inputs):
        return [list(self._vector) for _ in inputs]

    async def embed_one(self, text):
        self.queries.append(text)
        return list(self._vector)


class BoomEmbedder:
    async def embed(self, inputs):
        raise RuntimeError("embed down")

    async def embed_one(self, text):
        raise RuntimeError("embed down")


def _service(*, library=None, blob=None, chunks=None, embedder=None, **overrides):
    settings = make_settings(document_understanding_enabled=True, **overrides)
    return DocumentRetrievalService(
        library=library or InMemoryDocumentLibraryRepository(),
        blob_store=blob or InMemoryBlobStore(),
        chunk_store=chunks,
        embedder=embedder,
        settings=settings,
    )


async def _seed_doc(
    library,
    blob,
    *,
    user="u1",
    status=DocumentStatus.ready,
    filename="report.pdf",
    summary="Quarterly revenue report",
    parsed="# Report\n\nRevenue grew twenty percent this quarter.",
    doc_id=None,
) -> UserDocument:
    doc = UserDocument(userId=user, filename=filename, status=status, summary=summary)
    if doc_id:
        doc.id = doc_id
    if parsed is not None:
        path = blob_path(user, doc.id, PARSED_NAME)
        await blob.put(path, parsed.encode("utf-8"), "text/markdown")
        doc.parsedPath = path
    await library.create_document(doc)
    return doc


async def _add_chunk(chunks, doc, *, content="Revenue grew twenty percent.", vector=None):
    rec = DocChunkRecord(
        user_id=doc.userId,
        document_id=doc.id,
        chunk_index=0,
        content=content,
        heading="Report",
        char_start=0,
        char_end=len(content),
    )
    await chunks.add_many([rec], [list(vector or [1.0, 0.0, 0.0])])


# --- Tier 1: summary cards ---
async def test_context_block_lists_ready_documents():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    svc = _service(library=library, blob=blob)
    doc = await _seed_doc(library, blob)

    block = await svc.context_block("u1", "how did revenue do?", nonce="abcd")

    assert "BEGIN LIBRARY abcd" in block
    assert "END LIBRARY abcd" in block
    assert f"id={doc.id}" in block
    assert "report.pdf" in block
    assert "Quarterly revenue report" in block


async def test_context_block_empty_when_no_ready_docs():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    svc = _service(library=library, blob=blob)
    await _seed_doc(library, blob, status=DocumentStatus.analyzing)

    assert await svc.context_block("u1", "anything", nonce="x1") == ""


async def test_context_block_empty_when_disabled_or_no_docs():
    svc = _service()
    assert await svc.context_block("u1", "q", nonce="x1") == ""


# --- Tier 2: RAG excerpts, status-gated ---
async def test_context_block_includes_rag_excerpt_for_ready_doc():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore()
    svc = _service(library=library, blob=blob, chunks=chunks, embedder=FakeEmbedder())
    doc = await _seed_doc(library, blob)
    await _add_chunk(chunks, doc, content="Revenue grew twenty percent.")

    block = await svc.context_block("u1", "revenue growth?", nonce="n9")

    assert "Relevant excerpts" in block
    assert "Revenue grew twenty percent." in block
    assert "report.pdf" in block  # citation by filename


async def test_rag_never_surfaces_chunk_of_nonready_doc():
    """A failed/analyzing doc with a stray chunk must never surface — search is
    scoped to ready ids. This is the invariant the ingest hardening enforces."""
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore()
    svc = _service(library=library, blob=blob, chunks=chunks, embedder=FakeEmbedder())
    # A non-ready document that nonetheless has an indexed chunk (a stray vector).
    ghost = await _seed_doc(
        library, blob, status=DocumentStatus.analyzing, filename="secret.pdf",
        summary="should not appear", parsed=None,
    )
    await _add_chunk(chunks, ghost, content="LEAKED SECRET CONTENT")

    block = await svc.context_block("u1", "secret?", nonce="n1")

    assert block == ""  # no ready docs at all → no context
    # And even alongside a ready doc, the ghost's chunk never appears.
    ready = await _seed_doc(library, blob, filename="ok.pdf", summary="fine")
    await _add_chunk(chunks, ready, content="Public information.")
    block2 = await svc.context_block("u1", "anything", nonce="n2")
    assert "LEAKED SECRET CONTENT" not in block2
    assert "secret.pdf" not in block2
    assert "Public information." in block2


async def test_context_block_best_effort_on_embed_failure():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore()
    svc = _service(library=library, blob=blob, chunks=chunks, embedder=BoomEmbedder())
    doc = await _seed_doc(library, blob)
    await _add_chunk(chunks, doc)

    # Tier 2 fails internally, but Tier 1 cards still render (never raises).
    block = await svc.context_block("u1", "q", nonce="n3")
    assert f"id={doc.id}" in block
    assert "Relevant excerpts" not in block


async def test_context_block_best_effort_on_repo_failure():
    class BoomRepo:
        async def list_documents(self, user_id):
            raise RuntimeError("cosmos down")

    svc = _service(library=BoomRepo())
    assert await svc.context_block("u1", "q", nonce="n4") == ""


# --- Tier 3: fetch_document ---
async def test_fetch_document_returns_windowed_content():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    svc = _service(library=library, blob=blob, document_fetch_max_chars=10)
    doc = await _seed_doc(library, blob, parsed="0123456789ABCDEFGHIJ")

    res = await svc.fetch_document("u1", doc.id, start=0)
    assert res["content"] == "0123456789"
    assert res["truncated"] is True
    assert res["next_start"] == 10
    assert res["total_chars"] == 20

    res2 = await svc.fetch_document("u1", doc.id, start=10)
    assert res2["content"] == "ABCDEFGHIJ"
    assert res2["truncated"] is False
    assert res2["next_start"] is None


async def test_fetch_document_rejects_nonready():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    svc = _service(library=library, blob=blob)
    doc = await _seed_doc(library, blob, status=DocumentStatus.analyzing)

    res = await svc.fetch_document("u1", doc.id)
    assert "error" in res
    assert "content" not in res


async def test_fetch_document_unknown_or_cross_user_is_not_found():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    svc = _service(library=library, blob=blob)
    doc = await _seed_doc(library, blob, user="owner")

    # Unknown id.
    assert "error" in await svc.fetch_document("u1", "nope")
    # Another user's id: no leak, just not-found.
    res = await svc.fetch_document("attacker", doc.id)
    assert "error" in res
    assert "content" not in res


async def test_fetch_document_missing_parsed_blob():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    svc = _service(library=library, blob=blob)
    doc = await _seed_doc(library, blob, parsed=None)  # ready but no parsed.md

    res = await svc.fetch_document("u1", doc.id)
    assert "error" in res


# --- chat_capability (Tier 3 tool wrapper) ---
def test_capability_schema_shape():
    svc = _service()
    tools, handlers = build_document_capability(service=svc, user_id="u1", nonce="z")
    assert len(tools) == 1
    fn = tools[0]["function"]
    assert fn["name"] == "fetch_document"
    assert fn["parameters"]["required"] == ["document_id"]
    assert "fetch_document" in handlers


async def test_capability_handler_fences_content():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    svc = _service(library=library, blob=blob)
    doc = await _seed_doc(library, blob, parsed="hello world")
    _, handlers = build_document_capability(service=svc, user_id="u1", nonce="q7")

    res = await handlers["fetch_document"]({"document_id": doc.id}, ctx=None)
    assert "BEGIN DOCUMENT q7" in res["content"]
    assert "hello world" in res["content"]
    assert "END DOCUMENT q7" in res["content"]


async def test_capability_handler_error_passthrough_and_budget():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    svc = _service(library=library, blob=blob)
    doc = await _seed_doc(library, blob, parsed="x")
    _, handlers = build_document_capability(service=svc, user_id="u1", nonce="q")
    handler = handlers["fetch_document"]

    assert "error" in await handler({"document_id": "missing"}, ctx=None)
    assert "error" in await handler({}, ctx=None)
    # Exhaust the per-turn read budget.
    for _ in range(MAX_FETCHES_PER_TURN):
        await handler({"document_id": doc.id}, ctx=None)
    exhausted = await handler({"document_id": doc.id}, ctx=None)
    assert "budget" in exhausted["error"]


# --- factory: disabled => None; shared IO parity ---
def test_build_document_retrieval_none_when_ingestor_none():
    settings = make_settings(document_understanding_enabled=False)
    assert build_document_retrieval(settings, ingestor=None) is None


async def test_retrieval_shares_ingestor_io():
    """A document written through the ingestor's stores is visible to the
    retrieval service built from that ingestor (shared in-memory IO)."""
    settings = make_settings(document_understanding_enabled=True)
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore()

    class _Usage:
        async def record_completion(self, **kwargs):
            return None

    ingestor = DocumentIngestor(
        library=library,
        blob_store=blob,
        settings=settings,
        usage=_Usage(),
        cu_client=None,
        embedder=FakeEmbedder(),
        chunk_store=chunks,
    )
    svc = build_document_retrieval(settings, ingestor=ingestor)
    assert svc is not None

    doc = await _seed_doc(ingestor.library, ingestor.blob, summary="shared doc")
    block = await svc.context_block("u1", "q", nonce="p1")
    assert "shared doc" in block
    fetched = await svc.fetch_document("u1", doc.id)
    assert "Revenue grew twenty percent" in fetched["content"]
