"""Cosmos-native semantic memory orchestration and explicit CRUD."""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from typing import TypeVar

from .base import Embedder
from .cosmos_store import (
    CosmosMemoryStore,
    MemoryConflictError,
    MemoryFenceConflict,
    MemoryState,
    operation_id,
    operation_ids,
)
from .formatting import format_memory_context
from .models import MemoryRecord
from .planner import MemoryPlan, MemoryPlanner
from .service import MemoryWriteOutcome
from .telemetry import emit_memory_operation

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class CosmosMemoryService:
    """Canonical Cosmos memory with planner writes and user-owned CRUD."""

    enabled = True
    supports_create = True
    supports_edit = True
    supports_delete = True

    def __init__(
        self,
        *,
        store: CosmosMemoryStore,
        embedder: Embedder,
        planner: MemoryPlanner,
        embedding_model: str,
        top_k: int = 5,
        min_score: float = 0.25,
        max_injected: int = 5,
        max_chars_per_item: int = 500,
        max_total_chars: int = 2_000,
        min_chars_to_store: int = 12,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._planner = planner
        self._embedding_model = embedding_model
        self._top_k = top_k
        self._min_score = min_score
        self._max_injected = max_injected
        self._max_chars_per_item = max_chars_per_item
        self._max_total_chars = max_total_chars
        self._min_chars_to_store = min_chars_to_store

    async def recall(self, user_id: str, query: str) -> list[MemoryRecord]:
        started = time.monotonic()
        if not query or not query.strip():
            emit_memory_operation("recall", "skipped", "cosmos", started, count=0)
            return []
        try:
            vector = await self._embedder.embed_one(query)
            if not vector:
                emit_memory_operation("recall", "skipped", "cosmos", started, count=0)
                return []
            hits = await self._store.search(user_id, vector, self._top_k)
        except Exception as exc:  # noqa: BLE001 - recall must not break chat
            logger.warning("cosmos memory recall failed (%s)", type(exc).__name__)
            emit_memory_operation("recall", "failed", "cosmos", started)
            return []
        records = [record for record in hits if (record.score or 0.0) >= self._min_score]
        emit_memory_operation("recall", "ok", "cosmos", started, count=len(records))
        return records

    async def remember(
        self, user_id: str, session_id: str | None, text: str
    ) -> MemoryWriteOutcome:
        """Plan-and-apply a durable memory write, reporting what actually happened.

        Three different things can leave the text unstored and they are NOT
        interchangeable: the planner can legitimately decline (``noop``), it can
        decide the new text falsifies an existing memory and delete that instead
        (``removed``), or the write can fail outright (``unavailable``). Failures
        are still swallowed here — memory must never break a chat turn — but they
        are no longer indistinguishable from a deliberate decline, because telling
        the model "already covered, do not retry" after an outage is a lie it will
        confidently repeat to the user.
        """
        started = time.monotonic()
        cleaned = (text or "").strip()
        if len(cleaned) < self._min_chars_to_store:
            emit_memory_operation("save", "skipped", "cosmos", started, count=0)
            return "noop"
        try:
            state = await self._store.capture_state(user_id)
            query_vector = await self._embedder.embed_one(cleaned)
            if not query_vector:
                # No vector means no candidate search and therefore no plan: the
                # write was never attempted, which is a failure, not a decline.
                emit_memory_operation("save", "failed", "cosmos", started)
                return "unavailable"
            candidates = await self._store.search(
                user_id, query_vector, 8, state=state
            )
            plan = await self._planner.plan(cleaned, candidates)
            applied = await self._apply_plan(
                user_id, session_id, plan, candidates, state
            )
        except Exception as exc:  # noqa: BLE001 - remember must not break chat
            logger.warning("cosmos memory remember failed (%s)", type(exc).__name__)
            emit_memory_operation("save", "failed", "cosmos", started)
            return "unavailable"
        if applied == "unavailable":
            emit_memory_operation("save", "failed", "cosmos", started)
            return applied
        changed = applied != "noop"
        emit_memory_operation(
            "save", "ok" if changed else "skipped", "cosmos", started, count=int(changed)
        )
        return applied

    async def _apply_plan(
        self,
        user_id: str,
        session_id: str | None,
        plan: MemoryPlan,
        candidates: Sequence[MemoryRecord],
        state: MemoryState,
    ) -> MemoryWriteOutcome:
        """Apply ``plan``, returning the outcome from the *caller's* point of view.

        Note this answers "was this fact saved", not "did the store change" — a
        delete mutates the store while storing nothing, so it reports ``removed``.
        Returning a bare "changed" bool here is what let a delete surface to the
        model as a successful save of text that was never written.
        """
        if plan.action == "noop":
            return "noop"
        by_id = {record.id: record for record in candidates}
        target = by_id.get(plan.memory_id or "")
        text = (plan.text or "").strip()
        vector: list[float] | None = None
        if plan.action in {"add", "update"}:
            vector = await self._embedder.embed_one(text)
            if not vector:
                # The plan intended to write and could not; the fact is lost.
                return "unavailable"

        if plan.action == "add":
            record = MemoryRecord(
                id=f"m-{uuid.uuid4().hex}",
                user_id=user_id,
                text=text,
                session_id=session_id,
                kind="fact",
                origin="implicit",
                locked=False,
                embedding_model=self._embedding_model,
            )
            await self._commit_with_stable_epoch(
                state,
                lambda fence: self._store.commit_create(
                    fence, record, vector or []
                ),
            )
            return "saved"

        # The planner named a memory that is gone, or one this path may not touch
        # (explicit or locked memories are user-owned). Declining is correct.
        if target is None or target.origin != "implicit" or target.locked:
            return "noop"
        if not target.etag:
            raise MemoryConflictError(target.id)

        if plan.action == "update":
            updated = MemoryRecord(
                id=target.id,
                user_id=user_id,
                text=text,
                session_id=target.session_id,
                document_id=target.document_id,
                kind=target.kind,
                created_at=target.created_at,
                updated_at=datetime.now(timezone.utc),
                version=target.version + 1,
                origin="implicit",
                locked=False,
                embedding_model=self._embedding_model,
            )
            await self._commit_with_stable_epoch(
                state,
                lambda fence: self._store.commit_update(
                    fence, updated, vector or [], expected_etag=target.etag or ""
                ),
            )
            return "saved"

        await self._commit_with_stable_epoch(
            state,
            lambda fence: self._store.commit_delete(
                fence, target.id, expected_etag=target.etag or ""
            ),
        )
        return "removed"

    async def create_memory(
        self,
        user_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> MemoryRecord:
        cleaned = self._validate_text(text)
        operation_id = None
        if idempotency_key is not None:
            memory_id, operation_id = operation_ids(user_id, idempotency_key)
            existing_id = await self._store.get_operation(
                user_id,
                operation_id,
                operation="create",
                memory_id=memory_id,
            )
            if existing_id is not None:
                return await self._store.get_memory(user_id, existing_id)
        else:
            memory_id = f"m-{uuid.uuid4().hex}"
        state = await self._store.capture_state(user_id)
        vector = await self._embedder.embed_one(cleaned)
        if not vector:
            raise RuntimeError("embedding service returned no vector")
        record = MemoryRecord(
            id=memory_id,
            user_id=user_id,
            text=cleaned,
            kind="explicit",
            origin="user",
            locked=True,
            embedding_model=self._embedding_model,
        )
        return await self._commit_with_stable_epoch(
            state,
            lambda fence: self._store.commit_create(
                fence, record, vector, operation_id=operation_id
            ),
        )

    async def update_memory(
        self,
        user_id: str,
        memory_id: str,
        text: str,
        *,
        expected_etag: str,
        idempotency_key: str | None = None,
    ) -> MemoryRecord:
        cleaned = self._validate_text(text)
        receipt_id = (
            operation_id(user_id, "update", memory_id, idempotency_key)
            if idempotency_key is not None
            else None
        )
        if receipt_id is not None:
            existing_id = await self._store.get_operation(
                user_id,
                receipt_id,
                operation="update",
                memory_id=memory_id,
            )
            if existing_id is not None:
                return await self._store.get_memory(user_id, existing_id)
        state = await self._store.capture_state(user_id)
        current = await self._store.get_memory(user_id, memory_id, state=state)
        if current.etag != expected_etag:
            raise MemoryConflictError(memory_id)
        vector = await self._embedder.embed_one(cleaned)
        if not vector:
            raise RuntimeError("embedding service returned no vector")
        updated = MemoryRecord(
            id=current.id,
            user_id=user_id,
            text=cleaned,
            session_id=current.session_id,
            document_id=current.document_id,
            kind="explicit" if current.kind != "document" else current.kind,
            created_at=current.created_at,
            updated_at=datetime.now(timezone.utc),
            version=current.version + 1,
            origin="user",
            locked=True,
            embedding_model=self._embedding_model,
        )
        return await self._commit_with_stable_epoch(
            state,
            lambda fence: self._store.commit_update(
                fence,
                updated,
                vector,
                expected_etag=expected_etag,
                operation_id=receipt_id,
            ),
        )

    async def delete_memory(
        self,
        user_id: str,
        memory_id: str,
        *,
        expected_etag: str | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        receipt_id = (
            operation_id(user_id, "delete", memory_id, idempotency_key)
            if idempotency_key is not None
            else None
        )
        if receipt_id is not None:
            existing_id = await self._store.get_operation(
                user_id,
                receipt_id,
                operation="delete",
                memory_id=memory_id,
            )
            if existing_id is not None:
                return True
        state = await self._store.capture_state(user_id)
        current = await self._store.get_memory(user_id, memory_id, state=state)
        if expected_etag is not None and current.etag != expected_etag:
            raise MemoryConflictError(memory_id)
        if not current.etag:
            raise MemoryConflictError(memory_id)
        return await self._commit_with_stable_epoch(
            state,
            lambda fence: self._store.commit_delete(
                fence,
                memory_id,
                expected_etag=current.etag or "",
                operation_id=receipt_id,
            ),
        )

    async def list_memories(
        self, user_id: str, *, limit: int = 100
    ) -> list[MemoryRecord]:
        return await self._store.list_memories(user_id, limit=limit)

    async def remember_document(
        self,
        user_id: str,
        *,
        items: Sequence[str],
        session_id: str | None = None,
        document_id: str | None = None,
    ) -> int:
        texts = [item.strip() for item in items if item and item.strip()]
        if not texts:
            return 0
        vectors = await self._embedder.embed(texts)
        records: list[MemoryRecord] = []
        usable_vectors: list[Sequence[float]] = []
        for text, vector in zip(texts, vectors):
            if not vector:
                continue
            records.append(
                MemoryRecord(
                    id=f"m-{uuid.uuid4().hex}",
                    user_id=user_id,
                    text=text,
                    session_id=session_id,
                    document_id=document_id,
                    kind="document",
                    origin="user",
                    locked=True,
                    embedding_model=self._embedding_model,
                )
            )
            usable_vectors.append(vector)
        if document_id is not None:
            return await self._store.replace_document(
                user_id, document_id, records, usable_vectors
            )
        stored = 0
        for record, vector in zip(records, usable_vectors):
            current = await self._store.capture_state(user_id)
            await self._commit_with_stable_epoch(
                current,
                lambda fence, item=record, item_vector=vector: self._store.commit_create(
                    fence, item, item_vector
                ),
            )
            stored += 1
        return stored

    async def forget_user(self, user_id: str) -> int:
        return await self._store.erase_user(user_id)

    async def forget_session(self, user_id: str, session_id: str) -> int:
        return await self._store.erase_session(user_id, session_id)

    async def forget_document(self, user_id: str, document_id: str) -> int:
        return await self._store.erase_document(user_id, document_id)

    async def delete_document_source(self, user_id: str, document_id: str) -> int:
        return await self._store.delete_document_source(user_id, document_id)

    def format_context(self, records: list[MemoryRecord]) -> str | None:
        return format_memory_context(
            records,
            max_injected=self._max_injected,
            max_chars_per_item=self._max_chars_per_item,
            max_total_chars=self._max_total_chars,
        )

    async def warmup(self) -> None:
        await self._store.ensure_ready()

    async def close(self) -> None:
        await self._store.close()

    async def _commit_with_stable_epoch(
        self,
        initial: MemoryState,
        operation: Callable[[MemoryState], Awaitable[_T]],
    ) -> _T:
        state = initial
        for _attempt in range(3):
            try:
                return await operation(state)
            except MemoryFenceConflict:
                current = await self._store.capture_state(state.user_id)
                if current.epoch != state.epoch:
                    raise MemoryConflictError(
                        "memory was forgotten while the operation was in flight"
                    ) from None
                state = current
        raise MemoryConflictError("memory changed concurrently")

    @staticmethod
    def _validate_text(text: str) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError("Memory text cannot be blank.")
        if len(cleaned) > 2_000:
            raise ValueError("Memory text cannot exceed 2000 characters.")
        return cleaned
