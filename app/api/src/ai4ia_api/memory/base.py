"""Store + embedder seams for the memory layer.

These Protocols define the *only* surface the legacy generic service depends on,
so its in-memory and pgvector stores remain interchangeable. Every store method
requires a ``user_id`` — isolation is structural, not a caller convention.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .models import MemoryRecord


@runtime_checkable
class Embedder(Protocol):
    """Turns text into vectors (one per input, order-aligned)."""

    async def embed(self, inputs: Sequence[str]) -> list[list[float]]: ...

    async def embed_one(self, text: str) -> list[float]: ...


@runtime_checkable
class MemoryStore(Protocol):
    """A per-user vector store. Every method is user-scoped by signature."""

    async def add(self, record: MemoryRecord, vector: Sequence[float]) -> None: ...

    async def search(
        self, user_id: str, query_vector: Sequence[float], top_k: int
    ) -> list[MemoryRecord]:
        """Return the ``top_k`` most similar records for ``user_id`` (only)."""
        ...

    async def erase_user(self, user_id: str) -> int:
        """Delete all of a user's memories; return how many were removed."""
        ...

    async def erase_session(self, user_id: str, session_id: str) -> int:
        """Delete a user's memories for one session; return how many removed."""
        ...

    async def erase_document(self, user_id: str, document_id: str) -> int:
        """Delete a user's memories saved from one document; return how many were
        removed. Enables idempotent re-save (replace) and forget-by-document.
        """
        ...
