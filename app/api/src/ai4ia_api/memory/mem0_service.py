"""Real-mem0 memory backend implementing :class:`MemoryServiceProtocol`.

This wraps the actual ``mem0ai`` library (LLM fact-extraction + consolidation)
so the chat layer is unchanged: ``Mem0MemoryService`` exposes the exact same
``recall``/``remember``/``forget_*``/``format_context``/``warmup``/``close``
surface as the custom :class:`MemoryService`, selected by ``memory_store=mem0``.

Key behaviors / design notes:

- **Lazy build off the event loop.** ``mem0``'s pgvector store connects and
  creates its table in ``__init__``, and the LLM/embedder clients are sync. The
  whole ``mem0`` object is therefore built once, under a lock, inside
  ``asyncio.to_thread`` (``warmup`` or first use), mirroring the custom store's
  lazy ``ensure_ready``. A build failure degrades to "no memory" and is retried
  on the next call (``forget`` is the exception — it surfaces failures).

- **Non-blocking calls, bounded.** ``AsyncMemory`` runs its sync providers via
  ``asyncio.to_thread``. We wrap each call in a timeout (so a slow gateway can't
  stall the best-effort chat path) and a small semaphore (its sync providers +
  the ephemeral SQLite history run in worker threads, where unbounded
  concurrency can cause "database is locked").

- **Scoping policy.** ``remember`` maps ``session_id -> run_id`` so recall is
  user-global (memories span sessions) while ``forget_session`` can target one
  session. A memory written with ``session_id=None`` is user-scoped only and is
  not removable by ``forget_session`` (by construction).

- **Scoping asymmetry.** ``mem0`` takes ``user_id``/``run_id`` as top-level
  kwargs on ``add``/``delete_all`` but requires a ``filters`` dict on
  ``search``/``get_all`` (top-level entity kwargs are rejected). Handled here.

``mem0`` is imported lazily (only when the backend is built) so the app and
tests run without it; tests inject a fake ``AsyncMemory`` via the ``factory``.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .formatting import format_memory_context
from .models import MemoryRecord

logger = logging.getLogger(__name__)

# Upper bound on how many memories we enumerate to report a forget() count.
# delete_all() itself returns no count, so we list-then-delete; a generous cap
# keeps the reported number honest at personal/demo scale without unbounded IO.
_FORGET_LIST_CAP = 1000


@dataclass
class Mem0Bundle:
    """A built mem0 object plus a sync closer for the resources it owns."""

    memory: Any
    close: Callable[[], None]


def build_mem0_config(
    *,
    endpoint: str,
    api_key: str | None,
    api_key_header: str = "Ocp-Apim-Subscription-Key",
    api_version: str,
    llm_deployment: str,
    embed_deployment: str,
    embedding_dims: int,
    collection_name: str,
    history_db_path: str,
    connection_pool: Any,
) -> dict[str, Any]:
    """Assemble the ``mem0`` config dict pointed at our APIM model gateway.

    Both the LLM and embedder use the ``azure_openai`` provider: ``api_key`` is
    the gateway key and ``default_headers`` carries it in ``api_key_header``
    (the openai SDK also sends it as ``api-key``; the configured gateway checks
    the explicit header, so the duplicate is harmless). The deployment
    name is both the path segment (``azure_deployment``) and the body ``model``.
    The pgvector store receives our pre-built AAD connection pool and is forced
    to an exact scan (``hnsw``/``diskann`` off) because 3072-dim embeddings
    exceed pgvector's 2000-dim ANN index ceiling.
    """
    azure_kwargs = {
        "azure_endpoint": endpoint,
        "api_version": api_version,
        "api_key": api_key,
        "default_headers": ({api_key_header: api_key} if api_key else None),
    }
    return {
        "llm": {
            "provider": "azure_openai",
            "config": {
                "model": llm_deployment,
                # mem0's defaults, set explicitly: the extraction model is
                # non-reasoning, so temperature + max_tokens are accepted.
                "temperature": 0.1,
                "max_tokens": 2000,
                "azure_kwargs": {"azure_deployment": llm_deployment, **azure_kwargs},
            },
        },
        "embedder": {
            "provider": "azure_openai",
            "config": {
                "model": embed_deployment,
                "embedding_dims": embedding_dims,
                "azure_kwargs": {"azure_deployment": embed_deployment, **azure_kwargs},
            },
        },
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "collection_name": collection_name,
                "embedding_model_dims": embedding_dims,
                "hnsw": False,
                "diskann": False,
                "connection_pool": connection_pool,
            },
        },
        "history_db_path": history_db_path,
        "version": "v1.1",
    }


def _results(payload: Any) -> list[dict[str, Any]]:
    """mem0 v1.1 returns ``{"results": [...]}`` from search/get_all."""
    if isinstance(payload, dict):
        items = payload.get("results", [])
    else:
        items = payload or []
    return [item for item in items if isinstance(item, dict)]


class Mem0MemoryService:
    """Per-user semantic memory backed by the real ``mem0`` library."""

    enabled = True

    def __init__(
        self,
        *,
        factory: Callable[[], Mem0Bundle],
        top_k: int = 5,
        search_threshold: float = 0.1,
        min_chars_to_store: int = 12,
        max_injected: int = 5,
        max_chars_per_item: int = 500,
        max_total_chars: int = 2000,
        add_timeout_s: float = 20.0,
        op_timeout_s: float = 12.0,
        max_concurrency: int = 4,
    ) -> None:
        # Built lazily off the event loop; the factory blocks (connects + creates
        # the table), so it runs inside asyncio.to_thread as its own task.
        self._factory = factory
        self._top_k = top_k
        self._search_threshold = search_threshold
        self._min_chars_to_store = min_chars_to_store
        self._max_injected = max_injected
        self._max_chars_per_item = max_chars_per_item
        self._max_total_chars = max_total_chars
        self._add_timeout_s = add_timeout_s
        self._op_timeout_s = op_timeout_s
        self._bundle: Mem0Bundle | None = None
        # A single in-flight build task shared by all waiters. The task owns the
        # assignment of ``_bundle`` and clears itself, so a caller whose await is
        # cancelled (e.g. a warmup timeout) never abandons the build nor lets a
        # second build start — the build keeps running and finishes once.
        self._build_task: asyncio.Task[Mem0Bundle] | None = None
        # Set once close() begins; gates new builds and tells an in-flight build
        # to discard (not publish) its bundle so it can't resurrect the service.
        self._closing = False
        self._build_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_concurrency)

    async def _build_bundle(self) -> Mem0Bundle:
        """Build the bundle off the event loop; owns ``_bundle``/``_build_task``.

        Runs as a standalone task (not tied to any caller) so it survives caller
        cancellation. All shared-state writes happen under ``_build_lock`` and
        only clear ``_build_task`` if it still refers to us, so a concurrent
        close()/retry can't be clobbered. If close() began while we were
        building, the freshly built bundle is discarded (closed) instead of
        published — preventing a post-shutdown resurrection/leak.
        """
        try:
            bundle = await asyncio.to_thread(self._factory)
        except BaseException:
            async with self._build_lock:
                if self._build_task is asyncio.current_task():
                    self._build_task = None
            raise
        async with self._build_lock:
            if self._build_task is asyncio.current_task():
                self._build_task = None
            if self._closing:
                doomed: Mem0Bundle | None = bundle
            else:
                self._bundle = bundle
                doomed = None
        if doomed is not None:
            try:
                await asyncio.to_thread(doomed.close)
            except Exception:  # noqa: BLE001 - discard close is best-effort
                logger.warning("mem0 discarded-build close failed", exc_info=True)
        return bundle

    async def _ensure(self) -> Any:
        """Return the built mem0 object, constructing it at most once.

        The lock guards only task creation (not the slow build), and the build
        is shielded so a cancelled waiter does not cancel it. Raises on build
        failure (best-effort callers swallow it; the build is then retried) and
        if the service is closing.
        """
        bundle = self._bundle
        if bundle is not None:
            return bundle.memory
        async with self._build_lock:
            if self._bundle is not None:
                return self._bundle.memory
            if self._closing:
                raise RuntimeError("memory service is closing")
            if self._build_task is None:
                self._build_task = asyncio.create_task(self._build_bundle())
            task = self._build_task
        bundle = await asyncio.shield(task)
        return bundle.memory

    async def _call(self, make_coro: Callable[[], Any], timeout: float) -> Any:
        """Run a mem0 op holding the semaphore for the op's REAL lifetime.

        On timeout (or if our caller is cancelled mid-await) the underlying task
        keeps running — and keeps its semaphore slot — until it actually
        finishes, so we never free the slot while a worker thread is still
        touching the SQLite history db or the gateway. Whenever we abandon the
        await before the task is done we attach a callback that consumes its
        eventual result/exception to avoid "Task exception was never retrieved".
        """
        async def _guarded() -> Any:
            async with self._sem:
                return await make_coro()

        task: asyncio.Task[Any] = asyncio.create_task(_guarded())
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except BaseException:
            if not task.done():
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
            elif not task.cancelled():
                task.exception()
            raise

    async def recall(self, user_id: str, query: str) -> list[MemoryRecord]:
        """Best-effort: return relevant memories, or [] on any failure.

        The lazy build is bounded by the op timeout too, so a cold-start build
        that hangs degrades to "no memory" for this turn (the build continues in
        the background and serves the next turn) rather than stalling chat.
        """
        if not query or not query.strip():
            return []
        try:
            mem = await asyncio.wait_for(self._ensure(), timeout=self._op_timeout_s)
            payload = await self._call(
                lambda: mem.search(
                    query,
                    top_k=self._top_k,
                    filters={"user_id": user_id},
                    threshold=self._search_threshold,
                ),
                self._op_timeout_s,
            )
        except Exception:  # noqa: BLE001 - memory must never break chat
            logger.warning("mem0 recall failed", exc_info=True)
            return []
        out: list[MemoryRecord] = []
        for item in _results(payload):
            text = (item.get("memory") or "").strip()
            if not text:
                continue
            kwargs: dict[str, Any] = {
                "user_id": user_id,
                "text": text,
                "session_id": item.get("run_id"),
                "score": item.get("score"),
            }
            mem_id = item.get("id")
            if mem_id is not None:
                kwargs["id"] = str(mem_id)
            out.append(MemoryRecord(**kwargs))
        return out

    async def remember(self, user_id: str, session_id: str | None, text: str) -> None:
        """Best-effort: extract + store from a durable user utterance.

        Unlike the custom store this triggers an LLM extraction call; it is
        timeout-bounded and fully swallowed so it can never break a chat turn.
        """
        cleaned = (text or "").strip()
        if len(cleaned) < self._min_chars_to_store:
            return
        add_kwargs: dict[str, Any] = {"user_id": user_id}
        if session_id:
            add_kwargs["run_id"] = session_id
        try:
            mem = await asyncio.wait_for(self._ensure(), timeout=self._op_timeout_s)
            await self._call(
                lambda: mem.add([{"role": "user", "content": cleaned}], **add_kwargs),
                self._add_timeout_s,
            )
        except Exception:  # noqa: BLE001 - memory must never break chat
            logger.warning("mem0 remember failed", exc_info=True)

    async def remember_document(
        self,
        user_id: str,
        *,
        items: Sequence[str],
        session_id: str | None = None,
        document_id: str | None = None,
    ) -> int:
        """Store document excerpts verbatim as durable memories.

        An *explicit* user action, so (unlike :meth:`remember`) failures are NOT
        swallowed. ``infer=False`` stores each excerpt as-is, bypassing mem0's
        LLM fact-extraction pass — document text is reference content to recall
        verbatim, not an utterance to distill. Returns how many items were
        stored.

        When ``document_id`` is given the excerpts are tagged with it
        (``metadata``) and any prior generation for that document is removed
        first, so a re-save is idempotent rather than duplicating."""
        texts = [t.strip() for t in items if t and t.strip()]
        if not texts:
            return 0
        add_kwargs: dict[str, Any] = {"user_id": user_id, "infer": False}
        if session_id:
            add_kwargs["run_id"] = session_id
        if document_id is not None:
            add_kwargs["metadata"] = {"document_id": document_id}
        mem = await asyncio.wait_for(self._ensure(), timeout=self._op_timeout_s)
        if document_id is not None:
            await self._forget_document(mem, user_id, document_id)
        stored = 0
        for text in texts:
            await self._call(
                lambda t=text: mem.add([{"role": "user", "content": t}], **add_kwargs),
                self._add_timeout_s,
            )
            stored += 1
        return stored

    async def forget_user(self, user_id: str) -> int:
        """Erase all of a user's memories (NOT swallowed — explicit deletion).

        The returned count is capped at ``_FORGET_LIST_CAP`` (the enumeration
        bound); ``delete_all`` still erases every matching row regardless.
        """
        mem = await asyncio.wait_for(self._ensure(), timeout=self._op_timeout_s)
        listing = await self._call(
            lambda: mem.get_all(filters={"user_id": user_id}, top_k=_FORGET_LIST_CAP),
            self._op_timeout_s,
        )
        count = len(_results(listing))
        await self._call(lambda: mem.delete_all(user_id=user_id), self._op_timeout_s)
        return count

    async def forget_session(self, user_id: str, session_id: str) -> int:
        """Erase a user's memories for one session (NOT swallowed)."""
        filters = {"user_id": user_id, "run_id": session_id}
        mem = await asyncio.wait_for(self._ensure(), timeout=self._op_timeout_s)
        listing = await self._call(
            lambda: mem.get_all(filters=filters, top_k=_FORGET_LIST_CAP),
            self._op_timeout_s,
        )
        count = len(_results(listing))
        await self._call(
            lambda: mem.delete_all(user_id=user_id, run_id=session_id),
            self._op_timeout_s,
        )
        return count

    async def forget_document(self, user_id: str, document_id: str) -> int:
        """Erase a user's memories saved from one document (NOT swallowed).

        mem0's ``delete_all`` only scopes by entity (user/run), not by custom
        metadata, so document-scoped deletion lists the user's memories and
        removes by id the ones whose ``metadata.document_id`` matches."""
        mem = await asyncio.wait_for(self._ensure(), timeout=self._op_timeout_s)
        return await self._forget_document(mem, user_id, document_id)

    async def _forget_document(self, mem: Any, user_id: str, document_id: str) -> int:
        """List the user's memories and delete by id those tagged with
        ``document_id``. Shared by the idempotent re-save and the explicit
        forget-by-document path; failures propagate (explicit deletion)."""
        listing = await self._call(
            lambda: mem.get_all(filters={"user_id": user_id}, top_k=_FORGET_LIST_CAP),
            self._op_timeout_s,
        )
        deleted = 0
        for item in _results(listing):
            metadata = item.get("metadata") or {}
            if metadata.get("document_id") != document_id:
                continue
            mem_id = item.get("id")
            if mem_id is None:
                continue
            await self._call(
                lambda i=mem_id: mem.delete(memory_id=i), self._op_timeout_s
            )
            deleted += 1
        return deleted

    def format_context(self, records: list[MemoryRecord]) -> str | None:
        """Render recalled records as a capped, untrusted-labelled context block."""
        return format_memory_context(
            records,
            max_injected=self._max_injected,
            max_chars_per_item=self._max_chars_per_item,
            max_total_chars=self._max_total_chars,
        )

    async def warmup(self) -> None:
        """Eagerly build mem0; best-effort (lazy retry on first use otherwise)."""
        try:
            await self._ensure()
        except Exception:  # noqa: BLE001 - warmup is best-effort/diagnostic
            logger.warning("mem0 warmup failed; will initialize lazily", exc_info=True)

    async def close(self) -> None:
        """Release the pool + credential mem0 owns (off the event loop).

        Sets ``_closing`` under the lock so no new build can start and any
        in-flight build discards its bundle instead of publishing it; then
        drains the in-flight build (bounded) and closes whatever bundle exists.
        Idempotent.
        """
        async with self._build_lock:
            self._closing = True
            task = self._build_task
            bundle = self._bundle
            self._bundle = None
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self._op_timeout_s)
            except BaseException:  # noqa: BLE001 - bounded drain; close anyway
                if not task.done():
                    task.add_done_callback(lambda t: t.cancelled() or t.exception())
            # The build saw _closing and closed its own (discarded) bundle, so
            # there is nothing new to close here; _bundle stays None.
        if bundle is None:
            return
        try:
            await asyncio.to_thread(bundle.close)
        except Exception:  # noqa: BLE001 - never let shutdown raise
            logger.warning("mem0 close failed", exc_info=True)
