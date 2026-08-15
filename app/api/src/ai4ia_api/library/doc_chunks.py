"""Per-user document chunk vector store.

Defines the :class:`DocChunkStore` protocol — a ``doc_chunks`` record carrying
``document_id`` plus grounding (filename, heading, char range) — and the
in-process :class:`InMemoryDocChunkStore` that backs local/dev and unit tests.
Search is always scoped to ``user_id`` and may be further restricted to a set of
``document_id`` values ("retrieval over these documents").

The durable implementation is
:class:`~ai4ia_api.library.ai_search_chunks.AzureSearchDocChunkStore`. A
Postgres/pgvector store lived here until it was removed with the PostgreSQL
server it required; Azure AI Search is now the only durable chunk index.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

@dataclass(slots=True)
class DocChunkRecord:
    user_id: str
    document_id: str
    chunk_index: int
    content: str
    heading: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    # Audio/video time grounding: the chunk's media start/end offset
    # in milliseconds and its speaker label, when known. ``None`` for documents.
    start_ms: int | None = None
    end_ms: int | None = None
    speaker: str | None = None
    id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    score: float | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"{self.user_id}:{self.document_id}:{self.chunk_index}"


@runtime_checkable
class DocChunkStore(Protocol):
    async def add_many(
        self, records: Sequence[DocChunkRecord], vectors: Sequence[Sequence[float]]
    ) -> None: ...

    async def search(
        self,
        user_id: str,
        query_vector: Sequence[float],
        top_k: int,
        *,
        document_ids: Sequence[str] | None = None,
        query_text: str | None = None,
    ) -> list[DocChunkRecord]: ...

    async def delete_document(self, user_id: str, document_id: str) -> int: ...

    async def close(self) -> None: ...


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
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


class InMemoryDocChunkStore:
    """Process-local doc-chunk store for local/dev + tests (python cosine scan)."""

    def __init__(self, *, expected_dim: int | None = None) -> None:
        self._expected_dim = expected_dim
        self._records: list[tuple[DocChunkRecord, list[float]]] = []

    def _check_dim(self, vector: Sequence[float]) -> None:
        if self._expected_dim is not None and len(vector) != self._expected_dim:
            raise ValueError(
                f"embedding dimension {len(vector)} != expected {self._expected_dim}"
            )

    async def add_many(
        self, records: Sequence[DocChunkRecord], vectors: Sequence[Sequence[float]]
    ) -> None:
        if len(records) != len(vectors):
            raise ValueError("records and vectors length mismatch")
        existing = {r.id for r, _ in self._records}
        for record, vector in zip(records, vectors):
            self._check_dim(vector)
            if record.id in existing:
                continue
            self._records.append((record, [float(x) for x in vector]))
            existing.add(record.id)

    async def search(
        self,
        user_id: str,
        query_vector: Sequence[float],
        top_k: int,
        *,
        document_ids: Sequence[str] | None = None,
        query_text: str | None = None,
    ) -> list[DocChunkRecord]:
        # ``query_text`` is accepted for protocol parity with the Azure AI Search
        # backend (hybrid + semantic); this in-memory store is pure-vector and
        # ignores it.
        self._check_dim(query_vector)
        allow = set(document_ids) if document_ids is not None else None
        scored: list[tuple[float, DocChunkRecord]] = []
        for record, vector in self._records:
            if record.user_id != user_id:
                continue
            if allow is not None and record.document_id not in allow:
                continue
            score = _cosine(query_vector, vector)
            scored.append((score, record))
        # Sort by score desc, then a deterministic key (document_id, chunk_index).
        scored.sort(key=lambda item: (-item[0], item[1].document_id, item[1].chunk_index))
        out: list[DocChunkRecord] = []
        for score, record in scored[: max(0, top_k)]:
            out.append(
                DocChunkRecord(
                    user_id=record.user_id,
                    document_id=record.document_id,
                    chunk_index=record.chunk_index,
                    content=record.content,
                    heading=record.heading,
                    char_start=record.char_start,
                    char_end=record.char_end,
                    start_ms=record.start_ms,
                    end_ms=record.end_ms,
                    speaker=record.speaker,
                    id=record.id,
                    created_at=record.created_at,
                    score=score,
                )
            )
        return out

    async def delete_document(self, user_id: str, document_id: str) -> int:
        before = len(self._records)
        self._records = [
            item
            for item in self._records
            if not (item[0].user_id == user_id and item[0].document_id == document_id)
        ]
        return before - len(self._records)

    async def close(self) -> None:
        return None
