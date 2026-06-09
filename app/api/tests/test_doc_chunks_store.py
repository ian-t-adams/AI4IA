"""In-memory doc-chunk store (Phase 11B): per-user + per-document isolation,
cosine ranking, the document_ids filter, deletion, and dimension guards."""
from __future__ import annotations

import pytest

from ai4ia_api.library.doc_chunks import DocChunkRecord, InMemoryDocChunkStore


def _rec(user_id: str, document_id: str, index: int, text: str = "t") -> DocChunkRecord:
    return DocChunkRecord(
        user_id=user_id, document_id=document_id, chunk_index=index, content=text
    )


async def test_add_and_search_scoped_to_user():
    store = InMemoryDocChunkStore(expected_dim=2)
    await store.add_many([_rec("u1", "d1", 0)], [[1.0, 0.0]])
    await store.add_many([_rec("u2", "d9", 0)], [[1.0, 0.0]])

    hits = await store.search("u1", [1.0, 0.0], top_k=10)
    assert [h.user_id for h in hits] == ["u1"]
    assert hits[0].document_id == "d1"
    # u2's identical vector is never returned for u1.
    assert all(h.user_id == "u1" for h in hits)


async def test_ranking_by_cosine_similarity():
    store = InMemoryDocChunkStore(expected_dim=2)
    await store.add_many(
        [_rec("u1", "d1", 0, "near"), _rec("u1", "d1", 1, "far")],
        [[1.0, 0.0], [0.0, 1.0]],
    )
    hits = await store.search("u1", [1.0, 0.0], top_k=2)
    assert hits[0].content == "near"
    assert hits[0].score > hits[1].score


async def test_document_ids_filter():
    store = InMemoryDocChunkStore(expected_dim=2)
    await store.add_many(
        [_rec("u1", "d1", 0), _rec("u1", "d2", 0)],
        [[1.0, 0.0], [1.0, 0.0]],
    )
    hits = await store.search("u1", [1.0, 0.0], top_k=10, document_ids=["d2"])
    assert {h.document_id for h in hits} == {"d2"}


async def test_delete_document_removes_only_that_document():
    store = InMemoryDocChunkStore(expected_dim=2)
    await store.add_many(
        [_rec("u1", "d1", 0), _rec("u1", "d2", 0)],
        [[1.0, 0.0], [1.0, 0.0]],
    )
    removed = await store.delete_document("u1", "d1")
    assert removed == 1
    hits = await store.search("u1", [1.0, 0.0], top_k=10)
    assert {h.document_id for h in hits} == {"d2"}


async def test_dimension_mismatch_raises():
    store = InMemoryDocChunkStore(expected_dim=3)
    with pytest.raises(ValueError):
        await store.add_many([_rec("u1", "d1", 0)], [[1.0, 0.0]])


async def test_length_mismatch_raises():
    store = InMemoryDocChunkStore(expected_dim=2)
    with pytest.raises(ValueError):
        await store.add_many([_rec("u1", "d1", 0)], [])


async def test_duplicate_ids_are_ignored():
    store = InMemoryDocChunkStore(expected_dim=2)
    await store.add_many([_rec("u1", "d1", 0)], [[1.0, 0.0]])
    await store.add_many([_rec("u1", "d1", 0)], [[0.0, 1.0]])  # same id
    hits = await store.search("u1", [1.0, 0.0], top_k=10)
    assert len(hits) == 1
