"""Azure AI Search doc-chunk store: index bootstrap, document mapping,
filter construction, vector query shaping, hybrid + semantic ranking (with graceful
fallback), result mapping, and delete-by-filter.

The async ``SearchClient`` / ``SearchIndexClient`` are injected as fakes so the
store's logic is exercised without a live search service. The real azure index
models are still constructed by ``ensure_ready`` (pure, no network)."""
from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from ai4ia_api.library.ai_search_chunks import (
    AzureSearchDocChunkStore,
    _decode_key,
    _encode_key,
)
from ai4ia_api.library.doc_chunks import DocChunkRecord

_DIM = 3


class _Http400(Exception):
    """Stands in for the SDK's HttpResponseError: a 400 carrying a message."""

    status_code = 400


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


class _IndexingResult:
    def __init__(
        self,
        key: str,
        *,
        succeeded: bool,
        status_code: int,
        error_message: str | None = None,
    ) -> None:
        self.key = key
        self.succeeded = succeeded
        self.status_code = status_code
        self.error_message = error_message


class _PartialIndexSearchClient(_FakeSearchClient):
    async def merge_or_upload_documents(self, documents):
        docs = list(documents)
        self.uploaded.append(docs)
        return [
            _IndexingResult(docs[0]["key"], succeeded=True, status_code=200),
            _IndexingResult(
                docs[1]["key"],
                succeeded=False,
                status_code=503,
                error_message="service unavailable",
            ),
        ]


class _PartialDeleteSearchClient(_FakeSearchClient):
    async def delete_documents(self, documents):
        docs = list(documents)
        self.deleted.append(docs)
        return [
            _IndexingResult(docs[0]["key"], succeeded=True, status_code=200),
            _IndexingResult(
                docs[1]["key"],
                succeeded=False,
                status_code=503,
                error_message="delete unavailable",
            ),
        ]


class _FakeIndexClient:
    def __init__(self):
        self.created: list = []
        self.closed = False

    async def create_or_update_index(self, index):
        self.created.append(index)
        return index

    async def close(self):
        self.closed = True


def _store(
    search_client=None,
    index_client=None,
    expected_dim=_DIM,
    semantic_ranking=True,
    time_source=None,
    per_user_index=True,
):
    return AzureSearchDocChunkStore(
        endpoint="https://example.search.windows.net",
        index_name="ai4ia-doc-chunks",
        expected_dim=expected_dim,
        semantic_ranking=semantic_ranking,
        per_user_index=per_user_index,
        search_client=search_client or _FakeSearchClient(),
        index_client=index_client or _FakeIndexClient(),
        time_source=time_source,
    )


class _Clock:
    """Manually advanced monotonic clock for the semantic backoff tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _RecoverableSemanticSearchClient(_FakeSearchClient):
    """Semantic fails until ``heal_after`` semantic attempts have been made."""

    def __init__(self, results=None, heal_after: int = 1) -> None:
        super().__init__(results)
        self.heal_after = heal_after
        self.semantic_attempts = 0

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        if kwargs.get("query_type") == "semantic":
            self.semantic_attempts += 1
            if self.semantic_attempts <= self.heal_after:
                raise RuntimeError("semantic ranker quota exceeded")
        return _FakeResults(self.results)


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
    await store.ensure_ready("u1")
    await store.ensure_ready("u1")  # idempotent / single-flight
    assert len(index_client.created) == 1
    index = index_client.created[0]
    # Per-user tenancy: the created index is the caller's, not the bare prefix.
    assert index.name == store.index_name_for_user("u1")
    assert index.name.startswith("ai4ia-doc-chunks-u")
    assert index.name != "ai4ia-doc-chunks"
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


async def test_add_many_accepts_when_every_indexing_result_succeeds():
    search_client = _FakeSearchClient()
    store = _store(search_client=search_client)

    await store.add_many(
        [_rec(index=0), _rec(index=1)],
        [[0.1, 0.2, 0.3], [0.3, 0.2, 0.1]],
    )

    assert len(search_client.uploaded[0]) == 2


async def test_add_many_raises_when_any_indexing_result_fails():
    search_client = _PartialIndexSearchClient()
    store = _store(search_client=search_client)

    with pytest.raises(RuntimeError, match=r"indexed 1/2.*service unavailable"):
        await store.add_many(
            [_rec(index=0), _rec(index=1)],
            [[0.1, 0.2, 0.3], [0.3, 0.2, 0.1]],
        )

    assert len(search_client.uploaded[0]) == 2


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


async def test_delete_document_raises_when_any_indexing_result_fails():
    search_client = _PartialDeleteSearchClient(
        results=[{"key": "k1"}, {"key": "k2"}]
    )
    store = _store(search_client=search_client)

    with pytest.raises(RuntimeError, match=r"deleted 1/2.*delete unavailable"):
        await store.delete_document("u1", "d1")

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
    await store.ensure_ready("u1")
    await store.close()
    # Injected clients are caller-owned: close must not tear them down.
    assert search_client.closed is False
    assert index_client.closed is False


async def test_ensure_ready_index_includes_semantic_config():
    index_client = _FakeIndexClient()
    store = _store(index_client=index_client)
    await store.ensure_ready("u1")
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


async def test_repeated_semantic_failures_stop_retrying_semantic_per_query():
    """An exhausted semantic quota must not cost a doomed round-trip per query.

    Search's *free* semantic plan is capped at 1,000 queries/month, after which
    every semantic query fails. Without a cooldown each retrieval paid a failed
    semantic call plus a stack trace before falling back to hybrid -- silently,
    for the rest of the month. Only the first query may attempt semantic.
    """
    search_client = _FailSemanticSearchClient()
    clock = _Clock()
    store = _store(search_client=search_client, time_source=clock)

    for _ in range(5):
        await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="revenue")

    semantic_calls = [
        c for c in search_client.search_calls if c.get("query_type") == "semantic"
    ]
    hybrid_calls = [
        c for c in search_client.search_calls if c.get("query_type") is None
    ]
    assert len(semantic_calls) == 1
    assert len(hybrid_calls) == 5


async def test_semantic_is_retried_after_the_cooldown_elapses():
    search_client = _FailSemanticSearchClient()
    clock = _Clock()
    store = _store(search_client=search_client, time_source=clock)

    await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="q")
    clock.advance(299.0)
    await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="q")
    assert sum(
        1 for c in search_client.search_calls if c.get("query_type") == "semantic"
    ) == 1

    clock.advance(2.0)  # past the 300s initial cooldown
    await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="q")
    assert sum(
        1 for c in search_client.search_calls if c.get("query_type") == "semantic"
    ) == 2


async def test_semantic_cooldown_backs_off_then_resets_after_recovery():
    """Consecutive failures back off; a success restores immediate semantic use."""
    search_client = _RecoverableSemanticSearchClient(heal_after=2)
    clock = _Clock()
    store = _store(search_client=search_client, time_source=clock)

    await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="q")  # fail 1
    clock.advance(301.0)
    await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="q")  # fail 2
    # Second failure doubles the cooldown to 600s, so 301s is not yet enough.
    clock.advance(301.0)
    await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="q")
    assert search_client.semantic_attempts == 2

    clock.advance(300.0)  # now past the 600s cooldown; this attempt succeeds
    await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="q")
    assert search_client.semantic_attempts == 3

    # Recovery clears the backoff: the very next query goes semantic again.
    await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="q")
    assert search_client.semantic_attempts == 4


async def test_semantic_backoff_does_not_suppress_pure_vector_queries():
    """A tripped breaker must not change no-text (pure vector) retrieval."""
    search_client = _FailSemanticSearchClient()
    clock = _Clock()
    store = _store(search_client=search_client, time_source=clock)

    await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="q")
    search_client.search_calls.clear()
    await store.search("u1", [1.0, 0.0, 0.0], top_k=4)

    assert len(search_client.search_calls) == 1
    assert search_client.search_calls[0]["search_text"] is None


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


class _BadRequestSemanticSearchClient(_FakeSearchClient):
    """Semantic fails with a request-specific 400, not a service outage."""

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        if kwargs.get("query_type") == "semantic":
            err = RuntimeError("invalid semantic query")
            err.status_code = 400
            raise err
        return _FakeResults(self.results)


async def test_query_specific_semantic_rejection_does_not_open_the_shared_breaker():
    """The breaker is process-wide, so one bad query must not downgrade everyone.

    A 400 is about that request; only service-level conditions (403 quota, 429,
    5xx, transport) may suppress semantic ranking for other users.
    """
    search_client = _BadRequestSemanticSearchClient()
    clock = _Clock()
    store = _store(search_client=search_client, time_source=clock)

    for _ in range(3):
        await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="q")

    semantic_calls = [
        c for c in search_client.search_calls if c.get("query_type") == "semantic"
    ]
    assert len(semantic_calls) == 3


async def test_concurrent_semantic_failures_advance_the_backoff_once():
    """One outage wave is one trip, not one per in-flight query."""
    search_client = _FailSemanticSearchClient()
    clock = _Clock()
    store = _store(search_client=search_client, time_source=clock)

    await asyncio.gather(
        *(
            store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="q")
            for _ in range(4)
        )
    )

    # Still the initial 300s cooldown, not 300*2^4.
    clock.advance(301.0)
    await store.search("u1", [1.0, 0.0, 0.0], top_k=4, query_text="q")
    semantic_calls = [
        c for c in search_client.search_calls if c.get("query_type") == "semantic"
    ]
    assert len(semantic_calls) == 2

# --- per-user index tenancy -----------------------------------------------------


def test_index_name_is_per_user_deterministic_and_name_safe():
    from ai4ia_api.library.ai_search_chunks import index_name_for

    a1 = index_name_for("ai4ia-doc-chunks", "user-a")
    a2 = index_name_for("ai4ia-doc-chunks", "user-a")
    b = index_name_for("ai4ia-doc-chunks", "user-b")

    # Deterministic: the same user must resolve to the same index every process
    # start, or their documents become unreachable after a restart.
    assert a1 == a2
    # Distinct users never collide -- a collision is a cross-tenant read.
    assert a1 != b
    # Azure's rules: 2-128 chars, lowercase, starts alphanumeric, no consecutive
    # dashes/underscores.
    for name in (a1, b):
        assert 2 <= len(name) <= 128
        assert name == name.lower()
        assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*[a-z0-9]", name)
        assert "--" not in name and "__" not in name
    # The raw user id must not appear in a control-plane-visible resource name.
    assert "user-a" not in a1


def test_index_name_rejects_a_prefix_that_would_be_invalid():
    from ai4ia_api.library.ai_search_chunks import index_name_for

    for bad in ("Has-Upper", "-leading", "has.dot", "has/slash", "x" * 200):
        with pytest.raises(ValueError):
            index_name_for(bad, "u1")


def test_shared_index_mode_still_uses_one_index():
    """Control: the per-user naming above is a *choice*, not the only behaviour.

    Without this, the per-user assertions could pass simply because the store
    always appends a suffix, and the escape hatch operators are told to use
    when they hit the tier ceiling would be silently broken.
    """
    store = _store(per_user_index=False)
    assert store.index_name_for_user("u1") == "ai4ia-doc-chunks"
    assert store.index_name_for_user("u2") == "ai4ia-doc-chunks"


async def test_each_user_gets_their_own_index():
    index_client = _FakeIndexClient()
    store = _store(index_client=index_client)

    await store.ensure_ready("u1")
    await store.ensure_ready("u2")
    await store.ensure_ready("u1")  # already created

    created = [i.name for i in index_client.created]
    assert len(created) == 2, created
    assert created[0] != created[1]
    assert set(created) == {
        store.index_name_for_user("u1"),
        store.index_name_for_user("u2"),
    }


async def test_add_many_refuses_a_batch_spanning_two_users():
    """A mixed batch has no single destination index.

    Harmless under one shared index; under per-user tenancy it would write one
    user's chunks into another user's index.
    """
    store = _store(search_client=_FakeSearchClient())
    mine = _rec(text="mine")
    theirs = _rec(text="theirs")
    theirs.user_id = "someone-else"

    with pytest.raises(ValueError, match="exactly one user"):
        await store.add_many([mine, theirs], [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])


async def test_single_user_batch_is_accepted():
    """Control for the guard above: it rejects *mixed* batches, not all batches."""
    search_client = _FakeSearchClient()
    store = _store(search_client=search_client)
    await store.add_many([_rec(text="a"), _rec(text="b")], [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
    assert search_client.uploaded


async def test_index_quota_exhaustion_is_named_not_a_bare_400():
    """The tier's index limit is a ceiling on users under this tenancy model.

    Azure reports it as a generic 400 that never mentions tenancy, which sends
    an operator to the wrong runbook.
    """
    from ai4ia_api.library.ai_search_chunks import SearchIndexQuotaError

    class _QuotaIndexClient:
        closed = False

        async def create_or_update_index(self, index):
            raise _Http400(
                "Cannot create more than 50 indexes in this service. "
                "The index quota has been exceeded."
            )

        async def close(self):
            self.closed = True

    store = _store(index_client=_QuotaIndexClient())
    with pytest.raises(SearchIndexQuotaError) as excinfo:
        await store.ensure_ready("u1")
    message = str(excinfo.value)
    assert "index slots" in message
    # Actionable: names both ways out.
    assert "AI4IA_SEARCH_INDEX_PER_USER=false" in message
    assert "50" in message


async def test_an_unrelated_400_is_not_relabelled_as_a_quota_problem():
    """Control: the quota detector must be narrow.

    Relabelling every 400 would tell an operator to raise their tier when the
    real problem was, say, a malformed field definition.
    """
    from ai4ia_api.library.ai_search_chunks import SearchIndexQuotaError

    class _BadRequestIndexClient:
        async def create_or_update_index(self, index):
            raise _Http400("The request is invalid. Field 'embedding' is malformed.")

        async def close(self):
            pass

    store = _store(index_client=_BadRequestIndexClient())
    with pytest.raises(_Http400):
        await store.ensure_ready("u1")
    # And specifically NOT the quota error.
    try:
        await store.ensure_ready("u1")
    except SearchIndexQuotaError:  # pragma: no cover - would be the bug
        raise AssertionError("unrelated 400 was relabelled as an index-quota error")
    except _Http400:
        pass


async def test_search_still_filters_by_user_even_with_a_dedicated_index():
    """Defense in depth: isolation must not rest on index routing alone.

    If index resolution were ever wrong, the OData filter is the second gate
    that still refuses another user's rows.
    """
    search_client = _FakeSearchClient()
    store = _store(search_client=search_client)
    await store.search("u1", [0.1, 0.2, 0.3], 3, query_text="q")

    assert search_client.search_calls, "expected a query to have been issued"
    assert "user_id eq 'u1'" in search_client.search_calls[-1]["filter"]


async def test_close_drains_every_per_user_client():
    """One client per user means close() must drain them all."""
    store = _store(index_client=_FakeIndexClient())
    built: list[Any] = []

    class _Client:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    async def fake_get(user_id: str):
        name = store.index_name_for_user(user_id)
        client = store._search_clients.get(name)
        if client is None:
            client = _Client()
            store._search_clients[name] = client
            built.append(client)
        return client

    await fake_get("u1")
    await fake_get("u2")
    assert len(built) == 2

    await store.close()
    assert all(c.closed for c in built)
    assert store._search_clients == {}