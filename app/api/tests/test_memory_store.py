"""Unit tests for the in-memory vector store: ranking, isolation, dim checks."""
from __future__ import annotations

import pytest

from ai4ia_api.memory.in_memory import InMemoryVectorStore, cosine_similarity
from ai4ia_api.memory.models import MemoryRecord


def test_cosine_similarity_edge_cases():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    # Empty / mismatched / zero vectors are defined as 0.0 (never blow up).
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


async def test_search_ranks_by_similarity_and_caps_top_k():
    store = InMemoryVectorStore()
    await store.add(MemoryRecord(user_id="u1", text="cats"), [1.0, 0.0])
    await store.add(MemoryRecord(user_id="u1", text="dogs"), [0.0, 1.0])
    await store.add(MemoryRecord(user_id="u1", text="kittens"), [0.9, 0.1])

    hits = await store.search("u1", [1.0, 0.0], top_k=2)
    assert [h.text for h in hits] == ["cats", "kittens"]
    assert hits[0].score >= hits[1].score


async def test_search_is_user_isolated():
    store = InMemoryVectorStore()
    await store.add(MemoryRecord(user_id="u1", text="u1 secret"), [1.0, 0.0])
    await store.add(MemoryRecord(user_id="u2", text="u2 secret"), [1.0, 0.0])

    hits = await store.search("u2", [1.0, 0.0], top_k=10)
    assert [h.text for h in hits] == ["u2 secret"]


async def test_search_does_not_mutate_stored_records():
    store = InMemoryVectorStore()
    record = MemoryRecord(user_id="u1", text="hello")
    await store.add(record, [1.0, 0.0])
    await store.search("u1", [1.0, 0.0], top_k=1)
    # The stored record's score stays None; only the returned copy is annotated.
    assert record.score is None


async def test_add_rejects_wrong_dimension():
    store = InMemoryVectorStore(expected_dim=3)
    with pytest.raises(ValueError):
        await store.add(MemoryRecord(user_id="u1", text="x"), [1.0, 2.0])


async def test_erase_user_and_session_counts():
    store = InMemoryVectorStore()
    await store.add(MemoryRecord(user_id="u1", text="a", session_id="s1"), [1.0, 0.0])
    await store.add(MemoryRecord(user_id="u1", text="b", session_id="s2"), [0.0, 1.0])

    assert await store.erase_session("u1", "s1") == 1
    remaining = await store.search("u1", [0.0, 1.0], top_k=10)
    assert [h.text for h in remaining] == ["b"]

    assert await store.erase_user("u1") == 1
    assert await store.search("u1", [0.0, 1.0], top_k=10) == []
