"""Memory orchestration: embed + store + recall, plus context formatting.

The chat router depends only on :class:`MemoryServiceProtocol`. When memory is
off it gets a :class:`NoopMemoryService`, so call sites stay unconditional and
behavior cannot drift between "enabled" and "disabled" code paths.

Safety posture (per design review):
- Recall and remember are best-effort: failures log and degrade to "no memory",
  never failing the chat turn. Explicit ``forget`` is *not* swallowed — the user
  asked to delete, so they must learn whether it worked.
- Recalled snippets are injected as clearly-delimited, explicitly *untrusted*
  reference material with hard caps (count, per-item chars, total chars) so a
  poisoned memory cannot escalate into instructions or bloat the context.
- Only durable user utterances are remembered (not assistant/tool output) and
  trivially short ones are skipped.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Literal, Protocol

from .base import Embedder, MemoryStore
from .formatting import format_memory_context
from .models import MemoryRecord
from .telemetry import emit_memory_operation

logger = logging.getLogger(__name__)

# What a remember() attempt actually did. This is deliberately NOT a bool: the
# three "nothing was stored" cases are not interchangeable, and collapsing them
# is how a failed write comes to be reported to the model as a benign no-op.
#
#   saved       - the text is now durably stored (added, or updated in place).
#   noop        - deliberately declined: too short, or already covered by an
#                 existing memory. Correct behaviour; retrying is pointless.
#   removed     - the text was NOT stored; it falsified an existing memory, which
#                 was deleted instead. A store mutation, but not a save.
#   unavailable - the write could not be performed (outage, conflict, embedder
#                 failure). Swallowed so a chat turn never breaks, but reported
#                 so an agent-callable caller can say so instead of claiming
#                 the fact was remembered.
MemoryWriteOutcome = Literal["saved", "noop", "removed", "unavailable"]


class MemoryServiceProtocol(Protocol):
    """What the chat layer needs from memory (real or no-op)."""

    @property
    def enabled(self) -> bool: ...

    async def recall(self, user_id: str, query: str) -> list[MemoryRecord]: ...

    # Reports what the write actually did. Callers on the passive path ignore it
    # (a skipped save must never disturb a turn); the agent-callable
    # ``remember_memory`` tool needs it, because reporting "saved" for a write that
    # was skipped or failed is a silent lie the model would repeat to the user.
    # Implementations must never raise: return ``"unavailable"`` instead.
    async def remember(
        self, user_id: str, session_id: str | None, text: str
    ) -> MemoryWriteOutcome: ...

    async def remember_document(
        self,
        user_id: str,
        *,
        items: Sequence[str],
        session_id: str | None = None,
        document_id: str | None = None,
    ) -> int: ...

    async def forget_user(self, user_id: str) -> int: ...

    async def forget_session(self, user_id: str, session_id: str) -> int: ...

    async def forget_document(self, user_id: str, document_id: str) -> int: ...

    def format_context(self, records: list[MemoryRecord]) -> str | None: ...

    async def warmup(self) -> None: ...

    async def close(self) -> None: ...


class NoopMemoryService:
    """Disabled memory: every operation is a safe no-op."""

    enabled = False

    async def recall(self, user_id: str, query: str) -> list[MemoryRecord]:
        return []

    async def remember(
        self, user_id: str, session_id: str | None, text: str
    ) -> MemoryWriteOutcome:
        # "unavailable", not "noop": memory being switched off means the fact was
        # genuinely not kept. "noop" would tell an agent-callable caller the text
        # was already covered, which is the one answer that is never true here.
        return "unavailable"

    async def remember_document(
        self,
        user_id: str,
        *,
        items: Sequence[str],
        session_id: str | None = None,
        document_id: str | None = None,
    ) -> int:
        return 0

    async def forget_user(self, user_id: str) -> int:
        return 0

    async def forget_session(self, user_id: str, session_id: str) -> int:
        return 0

    async def forget_document(self, user_id: str, document_id: str) -> int:
        return 0

    def format_context(self, records: list[MemoryRecord]) -> str | None:
        return None

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


class MemoryService:
    """Embed-backed semantic memory over a pluggable :class:`MemoryStore`."""

    enabled = True

    def __init__(
        self,
        *,
        store: MemoryStore,
        embedder: Embedder,
        top_k: int = 5,
        min_score: float = 0.25,
        max_injected: int = 5,
        max_chars_per_item: int = 500,
        max_total_chars: int = 2000,
        min_chars_to_store: int = 12,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._top_k = top_k
        self._min_score = min_score
        self._max_injected = max_injected
        self._max_chars_per_item = max_chars_per_item
        self._max_total_chars = max_total_chars
        self._min_chars_to_store = min_chars_to_store

    async def recall(self, user_id: str, query: str) -> list[MemoryRecord]:
        """Best-effort: return relevant memories, or [] on any failure."""
        started = time.monotonic()
        if not query or not query.strip():
            emit_memory_operation("recall", "skipped", "custom", started, count=0)
            return []
        try:
            vector = await self._embedder.embed_one(query)
            if not vector:
                emit_memory_operation("recall", "skipped", "custom", started, count=0)
                return []
            hits = await self._store.search(user_id, vector, self._top_k)
        except Exception:  # noqa: BLE001 - memory must never break chat
            logger.warning("memory recall failed", exc_info=True)
            emit_memory_operation("recall", "failed", "custom", started)
            return []
        records = [h for h in hits if (h.score or 0.0) >= self._min_score]
        emit_memory_operation("recall", "ok", "custom", started, count=len(records))
        return records

    async def remember(
        self, user_id: str, session_id: str | None, text: str
    ) -> MemoryWriteOutcome:
        """Best-effort: store a durable user utterance. Skips trivia + failures.

        Never raises. Reports which of those happened, so an agent-callable caller
        can tell the user the truth rather than assuming success — and, just as
        importantly, so a failure is never described as "already covered".
        """
        started = time.monotonic()
        cleaned = (text or "").strip()
        if len(cleaned) < self._min_chars_to_store:
            emit_memory_operation("save", "skipped", "custom", started, count=0)
            return "noop"
        try:
            vector = await self._embedder.embed_one(cleaned)
            if not vector:
                # `cleaned` is non-empty by the check above, so an empty vector is
                # an embedder that could not answer — the fact is lost, which is a
                # failure and not a decision to decline.
                emit_memory_operation("save", "failed", "custom", started)
                return "unavailable"
            record = MemoryRecord(user_id=user_id, session_id=session_id, text=cleaned)
            await self._store.add(record, vector)
        except Exception:  # noqa: BLE001 - memory must never break chat
            logger.warning("memory remember failed", exc_info=True)
            emit_memory_operation("save", "failed", "custom", started)
            return "unavailable"
        emit_memory_operation("save", "ok", "custom", started, count=1)
        return "saved"

    async def remember_document(
        self,
        user_id: str,
        *,
        items: Sequence[str],
        session_id: str | None = None,
        document_id: str | None = None,
    ) -> int:
        """Store document excerpts as durable ``kind="document"`` memories.

        Unlike :meth:`remember` this is an *explicit*, user-initiated action over
        content the user owns, so it skips the trivia gate and does NOT swallow
        failures — the user asked to save, so they must learn whether it worked.
        Embeds the items as a batch, stores one record each, and returns how many
        were stored (blank items and zero vectors are skipped).

        When ``document_id`` is given the save is *idempotent*: any
        prior generation saved from the same document is replaced, so clicking
        "save to memory" twice does not accumulate duplicates. Embedding runs
        before the erase so a failed embed surfaces without first deleting the
        existing memories."""
        texts = [t.strip() for t in items if t and t.strip()]
        if not texts:
            return 0
        vectors = await self._embedder.embed(texts)
        if document_id is not None:
            await self._store.erase_document(user_id, document_id)
        stored = 0
        for text, vector in zip(texts, vectors):
            if not vector:
                continue
            record = MemoryRecord(
                user_id=user_id,
                session_id=session_id,
                text=text,
                kind="document",
                document_id=document_id,
            )
            await self._store.add(record, vector)
            stored += 1
        return stored

    async def forget_user(self, user_id: str) -> int:
        """Erase all of a user's memories (NOT swallowed — explicit deletion)."""
        return await self._store.erase_user(user_id)

    async def forget_session(self, user_id: str, session_id: str) -> int:
        """Erase a user's memories for one session (NOT swallowed)."""
        return await self._store.erase_session(user_id, session_id)

    async def forget_document(self, user_id: str, document_id: str) -> int:
        """Erase a user's memories saved from one document (NOT swallowed).

        The explicit counterpart to :meth:`remember_document` — lets a user undo
        a save-to-memory for one document."""
        return await self._store.erase_document(user_id, document_id)

    def format_context(self, records: list[MemoryRecord]) -> str | None:
        """Render recalled records as a capped, untrusted-labelled context block."""
        return format_memory_context(
            records,
            max_injected=self._max_injected,
            max_chars_per_item=self._max_chars_per_item,
            max_total_chars=self._max_total_chars,
        )

    async def warmup(self) -> None:
        """Eagerly initialize the store (e.g. open the pool + create schema).

        Best-effort: a store without an ``ensure_ready`` hook (the in-memory
        store) is a no-op. Errors propagate so the caller can decide whether to
        log-and-continue; the durable store also self-heals by retrying lazily.
        """
        ensure = getattr(self._store, "ensure_ready", None)
        if ensure is not None:
            await ensure()

    async def close(self) -> None:
        """Release store resources (connection pool, credential) on shutdown."""
        close = getattr(self._store, "close", None)
        if close is not None:
            await close()
