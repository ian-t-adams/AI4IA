"""In-process cosine-similarity memory store.

The default backend until pgvector is validated live. Records are physically
bucketed by ``user_id`` (not one global list filtered after the fact) so a
user can never structurally see another user's memories, and tests are clear.
Not durable across process restarts or replicas — that is the pgvector store's
job in the next increment.

Known limitations deferred to the durable (pgvector) increment, where DB
transactions resolve them cleanly:
- No isolation between a concurrent ``/forget`` and an in-flight ``remember``
  for the same user (a tiny window where a just-embedded record could land
  after a forget). Acceptable here because memories are rebuildable and this
  backend targets single-replica personal use.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from .models import MemoryRecord


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]; 0.0 for a zero/empty/mismatched vector."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class InMemoryVectorStore:
    """A per-user, in-process vector store.

    ``expected_dim`` (when set) rejects mismatched vectors at write time so a
    misconfigured embedding model surfaces immediately rather than silently
    returning zero-similarity results.
    """

    def __init__(self, expected_dim: int | None = None) -> None:
        # user_id -> list of (record, vector)
        self._by_user: dict[str, list[tuple[MemoryRecord, list[float]]]] = {}
        self._expected_dim = expected_dim

    async def add(self, record: MemoryRecord, vector: Sequence[float]) -> None:
        vec = list(vector)
        if self._expected_dim is not None and len(vec) != self._expected_dim:
            raise ValueError(
                f"embedding dimension {len(vec)} != expected {self._expected_dim}"
            )
        self._by_user.setdefault(record.user_id, []).append((record, vec))

    async def search(
        self, user_id: str, query_vector: Sequence[float], top_k: int
    ) -> list[MemoryRecord]:
        bucket = self._by_user.get(user_id, [])
        scored: list[MemoryRecord] = []
        for record, vector in bucket:
            score = cosine_similarity(query_vector, vector)
            # Return a copy so the caller's score annotation never mutates storage.
            hit = MemoryRecord(
                user_id=record.user_id,
                text=record.text,
                session_id=record.session_id,
                kind=record.kind,
                document_id=record.document_id,
                id=record.id,
                created_at=record.created_at,
                score=score,
            )
            scored.append(hit)
        scored.sort(key=lambda r: (r.score or 0.0), reverse=True)
        return scored[: max(0, top_k)]

    async def erase_user(self, user_id: str) -> int:
        removed = len(self._by_user.get(user_id, []))
        self._by_user.pop(user_id, None)
        return removed

    async def erase_session(self, user_id: str, session_id: str) -> int:
        bucket = self._by_user.get(user_id)
        if not bucket:
            return 0
        kept = [(r, v) for (r, v) in bucket if r.session_id != session_id]
        removed = len(bucket) - len(kept)
        self._by_user[user_id] = kept
        return removed

    async def erase_document(self, user_id: str, document_id: str) -> int:
        bucket = self._by_user.get(user_id)
        if not bucket:
            return 0
        kept = [(r, v) for (r, v) in bucket if r.document_id != document_id]
        removed = len(bucket) - len(kept)
        self._by_user[user_id] = kept
        return removed
