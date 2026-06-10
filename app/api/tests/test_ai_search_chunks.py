"""Azure AI Search doc-chunk store (Phase 11I): index bootstrap, document mapping,
filter construction, vector query shaping, hybrid + semantic ranking (with graceful
fallback), result mapping, and delete-by-filter.

The async ``SearchClient`` / ``SearchIndexClient`` are injected as fakes so the
store's logic is exercised without a live search service. The real azure index
models are still constructed by ``ensure_ready`` (pure, no network)."""
from __future__ import annotations

import pytest

from ai4ia_api.library.ai_search_chunks import (
    AzureSearchDocChunkStore,
    _decode_key,
    _encode_key,
)
from ai4ia_api.library.doc_chunks import DocChunkRecord

_DIM = 3


class _FakeResults:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        async def _gen():
            for doc in self._docs:
                yield doc

        return _gen()


class _FakeSearchClient:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.uploaded: list[list[dict]] = []
        self.deleted: list[list[dict]] = []
        self.search_calls: list[dict] = []
        self.closed = False

    async def merge_or_upload_documents(self, documents):
        docs = list(documents)
        self.uploaded.append(docs)
        return [{"key": d["key"], "status": True} for d in docs]

    async def delete_documents(self, documents):
        docs = list(documents)
        self.deleted.append(docs)
        return [{"key": d["key"], "status": True} for d in docs]

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return _FakeResults(self.results)

    async def close(self):
        self.closed = True


class _FailSemanticSearchClient(_FakeSearchClient):
    """A search client whose semantic query fails, exercising hybrid fallback."""

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        if kwargs.get("query_type") == "semantic":
            raise RuntimeError("semantic ranker not available on this tier")
        return _FakeResults(self.results)


class _FakeIndexClient:
    def __init__(self):
        self.created: list = []
        self.closed = False

    async def create_or_update_index(self, index):
        self.created.append(index)
        return index

    async def close(self):
        self.closed = True


def _store(search_client=None, index_client=None, expected_dim=_DIM, semantic_ranking=True):
    return AzureSearchDocChunkStore(
        endpoint="https://example.search.windows.net",
        index_name="ai4ia-doc-chunks",
        expected_dim=expected_dim,
        semantic_ranking=semantic_ranking,
        search_client=search_client or _FakeSearchClient(),
        index_client=index_client or _FakeIndexClient(),
    )


def _rec(user_id="u1", document_id="d1", index=0, text="t", **kw) -> DocChunkRecord:
    return DocChunkRecord(
        user_id=user_id, document_id=document_id, chunk_index=index, content=text, **kw
    )


def test_key_encode_decode_round_trips():
    raw = "user:1/x:doc-99:42"
    key = _encode_key(raw)
    # Key alphabet is restricted to letters/digits/-/_/= (valid AI Search keys).
    assert all(c.isalnum() or c in "-_=" for c in key)
    assert _decode_key(key) == raw


async def test_ensure_ready_creates_index_once_with_vector_field():
    index_client = _FakeIndexClient()
    store = _store(index_client=index_client)
    await store.ensure_ready()
    await store.ensure_ready()  # idempotent / single-flight
    assert len(index_client.created) == 1
    index = index_client.created[0]
    assert index.name == "ai4ia-doc-chunks"
    embedding = next(f for f in index.fields if f.name == "embedding")
    assert embedding.vector_search_dimensions == _DIM


async def test_add_many_uploads_encoded_documents():
    search_client = _FakeSearchClient()
    store = _store(search_client=search_client)
    rec = _rec(text="hello", heading="H1", start_ms=1000, end_ms=2000, speaker="S1")
    await store.add_many([rec], [[0.1, 0.2, 0.3]])

    assert len(search_client.uploaded) == 1
    doc = search_client.uploaded[0][0]
    assert doc["key"] == _encode_key(rec.id)
    assert doc["chunk_id"] == rec.id
    assert doc["user_id"] == "u1"
    assert doc["document_id"] == "d1"
    assert doc["content"] == "hello"
    assert doc["heading"] == "H1"
    assert doc["start_ms"] == 1000
    assert doc["speaker"] == "S1"
    assert doc["embedding"] == [0.1, 0.2, 0.3]
    assert isinstance(doc["created_at"], str)


async def test_add_many_length_mismatch_raises():
    store = _store()
    with pytest.raises(ValueError):
        await store.add_many([_rec()], [])


async def test_add_many_dimension_mismatch_raises():
    store = _store(expected_dim=3)
    with pytest.raises(ValueError):
        await store.add_many([_rec()], [[1.0, 0.0]])


async def test_search_builds_filter_and_maps_results():
    search_client = _FakeSearchClient(
        results=[
            {
                "chunk_id": "u1:d1:0",
                "user_id": "u1",
                "document_id": "d1",
                "chunk_index": 0,
                "content": "hello",
                "heading": "Intro",
                "start_ms": 1000,
                "end_ms": 4000,
                "speaker": "Speaker 1",
                "created_at": "2024-01-01T00:00:00+00:00",
                "@search.score": 0.83,
            }
        ]
    )
    store = _store(search_client=search_client)
    hits = await store.search("u1", [1.0, 0.0, 0.0], top_k=5, document_ids=["d1", "d2"])

    call = search_client.search_calls[0]
    assert call["filter"] == (
        "user_id eq 'u1' and (document_id eq 'd1' or document_id eq 'd2')"
    )
    assert call["top"] == 5
    assert "content" in call["select"]
    vq = call["vector_queries"][0]
    assert vq.k_nearest_neighbors == 5
    assert vq.fields == "embedding"

    assert len(hits) == 1
    hit = hits[0]
    assert hit.content == "hello"
    assert hit.document_id == "d1"
    assert hit.id == "u1:d1:0"
    assert hit.score == pytest.approx(0.83)
    assert hit.start_ms == 1000
    assert hit.speaker == "Speaker 1"


async def test_search_user_only_filter_when_no_document_ids():
    search_client = _FakeSearchClient(results=[])
    store = _store(search_client=search_client)
    await store.search("u9", [1.0, 0.0, 0.0], top_k=3)
    assert search_client.search_calls[0]["filter"] == "user_id eq 'u9'"


async def test_search_escapes_single_quotes_in_user_id():
    search_client = _FakeSearchClient(results=[])
    store = _store(search_client=search_client)
    await store.search("o'brien", [1.0, 0.0, 0.0], top_k=3)
    assert search_client.search_calls[0]["filter"] == "user_id eq 'o''brien'"


async def test_search_empty_document_ids_returns_empty_without_query():
    search_client = _FakeSearchClient(results=[{"chunk_id": "x"}])
    store = _store(search_client=search_client)
    hits = await store.search("u1", [1.0, 0.0, 0.0], top_k=5, document_ids=[])
    assert hits == []
    assert search_client.search_calls == []


async def test_search_top_k_zero_returns_empty_without_query():
    search_client = _FakeSearchClient(results=[{"chunk_id": "x"}])
    store = _store(search_client=search_client)
    hits = await store.search("u1", [1.0, 0.0, 0.0], top_k=0)
    assert hits == []
    assert search_client.search_calls == []


async def test_delete_document_collects_keys_then_deletes():
    search_client = _FakeSearchClient(results=[{"key": "k1"}, {"key": "k2"}])
    store = _store(search_client=search_client)
    removed = await store.delete_document("u1", "d1")
    assert removed == 2
    call = search_client.search_calls[0]
    assert call["filter"] == "user_id eq 'u1' and document_id eq 'd1'"
    assert call["select"] == ["key"]
    assert search_client.deleted == [[{"key": "k1"}, {"key": "k2"}]]


async def test_delete_document_no_matches_returns_zero():
    search_client = _FakeSearchClient(results=[])
    store = _store(search_client=search_client)
    removed = await store.delete_document("u1", "missing")
    assert removed == 0
    assert search_client.deleted == []


async def test_close_does_not_close_injected_clients():
    search_client = _FakeSearchClient()
    index_client = _FakeIndexClient()
    store = _store(search_client=search_client, index_client=index_client)
    await store.ensure_ready()
    await store.close()
    # Injected clients are caller-owned: close must not tear them down.
    assert search_client.closed is False
    assert index_client.closed is False


async def test_ensure_ready_index_includes_semantic_config():
    index_client = _FakeIndexClient()
    store = _store(index_client=index_client)
    await store.ensure_ready()
    index = index_client.created[0]
    assert index.semantic_search is not None
    cfg = index.semantic_search.configurations[0]
    assert cfg.name == "ai4ia-semantic"
    assert cfg.prioritized_fields.content_fields[0].field_name == "content"


async def test_search_with_query_text_runs_hybrid_semantic():
    search_client = _FakeSearchClient(results=[])
    store = _store(search_client=search_client)
    await store.search(
        "u1", [1.0, 0.0, 0.0], top_k=5, document_ids=["d1"], query_text="quarterly revenue"
    )
    call = search_client.search_calls[0]
    # Hybrid: keyword text alongside the vector query; semantic rerank requested.
    assert call["search_text"] == "quarterly revenue"
    assert call["query_type"] == "semantic"
    assert call["semantic_configuration_name"] == "ai4ia-semantic"
    assert call["vector_queries"][0].fields == "embedding"
    assert call["filter"] == "user_id eq 'u1' and (document_id eq 'd1')"


async def test_search_without_query_text_is_pure_vector():
    search_client = _FakeSearchClient(results=[])
    store = _store(search_client=search_client)
    await store.search("u1", [1.0, 0.0, 0.0], top_k=5)
    call = search_client.search_calls[0]
    assert call["search_text"] is None
    assert "query_type" not in call
    assert "semantic_configuration_name" not in call


async def test_search_blank_query_text_is_pure_vector():
    search_client = _FakeSearchClient(results=[])
    store = _store(search_client=search_client)
    await store.search("u1", [1.0, 0.0, 0.0], top_k=5, query_text="   ")
    call = search_client.search_calls[0]
    assert call["search_text"] is None
    assert "query_type" not in call


async def test_search_semantic_disabled_uses_plain_hybrid():
    search_client = _FakeSearchClient(results=[])
    store = _store(search_client=search_client, semantic_ranking=False)
    await store.search("u1", [1.0, 0.0, 0.0], top_k=5, query_text="hello world")
    call = search_client.search_calls[0]
    assert call["search_text"] == "hello world"
    assert "query_type" not in call


async def test_search_semantic_failure_falls_back_to_hybrid():
    search_client = _FailSemanticSearchClient(
        results=[
            {
                "chunk_id": "u1:d1:0",
                "user_id": "u1",
                "document_id": "d1",
                "chunk_index": 0,
                "content": "hit",
                "created_at": "2024-01-01T00:00:00+00:00",
                "@search.score": 0.5,
            }
        ]
    )
    store = _store(search_client=search_client)
    hits = await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="revenue")
    # Two calls: the failed semantic attempt, then the plain-hybrid retry.
    assert len(search_client.search_calls) == 2
    assert search_client.search_calls[0]["query_type"] == "semantic"
    assert "query_type" not in search_client.search_calls[1]
    assert search_client.search_calls[1]["search_text"] == "revenue"
    assert len(hits) == 1
    assert hits[0].content == "hit"


async def test_from_document_prefers_reranker_score():
    search_client = _FakeSearchClient(
        results=[
            {
                "chunk_id": "u1:d1:0",
                "user_id": "u1",
                "document_id": "d1",
                "chunk_index": 0,
                "content": "x",
                "created_at": "2024-01-01T00:00:00+00:00",
                "@search.score": 0.41,
                "@search.reranker_score": 3.2,
            }
        ]
    )
    store = _store(search_client=search_client)
    hits = await store.search("u1", [1.0, 0.0, 0.0], top_k=3, query_text="q")
    assert hits[0].score == pytest.approx(3.2)
