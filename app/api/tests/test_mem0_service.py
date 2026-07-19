"""Unit tests for the real-mem0 backend.

A fake ``AsyncMemory`` (injected through the ``factory`` callable) lets these run
without the ``mem0`` library or any database, covering: the result mapping,
best-effort swallowing, the scoping asymmetry (filters dict on search/get_all vs
top-level kwargs on add), the fail-closed forget/document-replace surface
(every destructive call raises :class:`MemoryEraseUnsupportedError` immediately
without touching mem0 -- see ``memory/mem0_service.py`` for why), lazy single
build + retry, the config-dict shape, endpoint normalization, and factory
backend selection.
"""
from __future__ import annotations

import pytest

from ai4ia_api.config import MemoryStoreKind, Settings
from ai4ia_api.memory.factory import build_memory_service
from ai4ia_api.memory.mem0_service import (
    Mem0Bundle,
    Mem0MemoryService,
    MemoryEraseUnsupportedError,
    build_mem0_config,
)
from ai4ia_api.memory.mem0_store import normalize_azure_openai_endpoint
from ai4ia_api.memory.models import MemoryRecord
from ai4ia_api.memory.service import NoopMemoryService


class FakeAsyncMemory:
    """Records calls and returns canned payloads, mimicking AsyncMemory v1.1."""

    def __init__(self, *, search_results=None, get_all_results=None, get_results=None) -> None:
        self._search_results = search_results or []
        self._get_all_results = get_all_results if get_all_results is not None else []
        self._get_results = get_results or {}
        self.search_calls: list[dict] = []
        self.add_calls: list[dict] = []
        self.get_all_calls: list[dict] = []
        self.get_calls: list[str] = []
        self.delete_by_id_calls: list[dict] = []

    async def search(self, query, *, top_k=20, filters=None, threshold=None, **kwargs):
        self.search_calls.append(
            {"query": query, "top_k": top_k, "filters": filters, "threshold": threshold}
        )
        return {"results": list(self._search_results)}

    async def add(self, messages, **kwargs):
        self.add_calls.append({"messages": messages, "kwargs": kwargs})
        return {"results": []}

    async def get_all(self, *, filters=None, top_k=20, **kwargs):
        self.get_all_calls.append({"filters": filters, "top_k": top_k})
        results = list(self._get_all_results)
        doc_id = (filters or {}).get("document_id")
        if doc_id is not None:
            # Mirrors mem0's pgvector store, which matches arbitrary metadata
            # keys server-side (not just user_id/agent_id/run_id) -- so a
            # document-scoped listing only ever returns that document's rows.
            results = [r for r in results if (r.get("metadata") or {}).get("document_id") == doc_id]
        # Mirrors a real backend's top_k cap, so tests can exercise the
        # multi-pass _forget_by_filter loop (a scope bigger than one page).
        return {"results": results[:top_k]}

    async def get(self, memory_id):
        self.get_calls.append(memory_id)
        return self._get_results.get(memory_id)

    async def delete(self, memory_id=None):
        self.delete_by_id_calls.append({"memory_id": memory_id})
        # Mirrors a real backend: a successful delete call actually removes
        # the row, so a subsequent get_all() no longer returns it.
        self._get_all_results = [r for r in self._get_all_results if r.get("id") != memory_id]
        return {"message": "ok"}


def _bundle(mem) -> Mem0Bundle:
    closed = {"n": 0}

    def _close() -> None:
        closed["n"] += 1

    b = Mem0Bundle(memory=mem, close=_close)
    b._closed = closed  # type: ignore[attr-defined]
    return b


def _service(mem, **overrides) -> Mem0MemoryService:
    calls = {"n": 0}

    def factory() -> Mem0Bundle:
        calls["n"] += 1
        return _bundle(mem)

    overrides.setdefault("min_chars_to_store", 12)
    svc = Mem0MemoryService(factory=factory, **overrides)
    svc._factory_calls = calls  # type: ignore[attr-defined]
    return svc


# --- recall -----------------------------------------------------------------

async def test_recall_maps_fields_and_uses_filters_dict():
    mem = FakeAsyncMemory(
        search_results=[
            {"id": 7, "memory": "  likes orange  ", "run_id": "s1", "score": 0.9},
            {"id": "abc", "memory": "uses Foundry", "run_id": None, "score": 0.4},
            {"id": 1, "memory": "   ", "score": 0.1},  # blank -> dropped
        ]
    )
    svc = _service(mem, top_k=5)
    hits = await svc.recall("u1", "what do I like")

    assert [h.text for h in hits] == ["likes orange", "uses Foundry"]
    assert hits[0].session_id == "s1"
    assert hits[0].score == 0.9
    assert hits[0].id == "7"  # coerced to str
    assert hits[1].session_id is None
    # Scoping: search takes a filters DICT + top_k, never a top-level user_id.
    call = mem.search_calls[0]
    assert call["filters"] == {"user_id": "u1"}
    assert call["top_k"] == 5
    assert call["query"] == "what do I like"


async def test_recall_blank_query_skips_search():
    mem = FakeAsyncMemory(search_results=[{"memory": "x"}])
    svc = _service(mem)
    assert await svc.recall("u1", "   ") == []
    assert mem.search_calls == []


async def test_recall_swallows_errors():
    class Boom(FakeAsyncMemory):
        async def search(self, *a, **k):
            raise RuntimeError("gateway down")

    svc = _service(Boom())
    assert await svc.recall("u1", "q") == []


# --- remember ---------------------------------------------------------------

async def test_remember_skips_short_text():
    mem = FakeAsyncMemory()
    svc = _service(mem, min_chars_to_store=12)
    await svc.remember("u1", "s1", "hi")
    assert mem.add_calls == []


async def test_remember_passes_user_and_run_id():
    mem = FakeAsyncMemory()
    svc = _service(mem)
    await svc.remember("u1", "s1", "I really love the color orange")
    call = mem.add_calls[0]
    assert call["messages"] == [
        {"role": "user", "content": "I really love the color orange"}
    ]
    assert call["kwargs"] == {"user_id": "u1", "run_id": "s1"}


async def test_remember_without_session_omits_run_id():
    mem = FakeAsyncMemory()
    svc = _service(mem)
    await svc.remember("u1", None, "I really love the color orange")
    assert mem.add_calls[0]["kwargs"] == {"user_id": "u1"}


async def test_remember_swallows_errors():
    class Boom(FakeAsyncMemory):
        async def add(self, *a, **k):
            raise RuntimeError("extract failed")

    svc = _service(Boom())
    # Must not raise.
    await svc.remember("u1", "s1", "a sufficiently long durable message")


# --- forget: fails closed, never touches mem0 -------------------------------
#
# An earlier revision replaced mem0's own swallowed delete_all() with
# application-level list-then-delete-by-id, re-querying until the scope
# converged to empty (round 11 acceptance). That approach was itself
# reverted: mem0 2.0.12 (pinned) keeps a full plaintext copy of every memory
# in its own history store that vector-store deletion never touches, and
# provides no cross-replica write fencing, so no amount of application-level
# scanning can make a multi-pass "list until empty" verification sound. Every
# destructive forget call therefore raises MemoryEraseUnsupportedError
# immediately, before any mem0 interaction at all -- see
# memory/mem0_service.py for the full rationale.


async def test_forget_user_raises_erase_unsupported_without_touching_mem0():
    mem = FakeAsyncMemory(get_all_results=[{"id": 1}, {"id": 2}, {"id": 3}])
    svc = _service(mem)
    with pytest.raises(MemoryEraseUnsupportedError):
        await svc.forget_user("u1")
    # Fails closed before any mem0 interaction: no list, no delete, no build.
    assert mem.get_all_calls == []
    assert mem.delete_by_id_calls == []
    assert svc._bundle is None


async def test_forget_session_raises_erase_unsupported_without_touching_mem0():
    mem = FakeAsyncMemory(get_all_results=[{"id": 1}])
    svc = _service(mem)
    with pytest.raises(MemoryEraseUnsupportedError):
        await svc.forget_session("u1", "s9")
    assert mem.get_all_calls == []
    assert mem.delete_by_id_calls == []
    assert svc._bundle is None


# --- 11E-3: document-scoped save (idempotent replace is now unsupported) ---


async def test_remember_document_without_id_does_not_list_or_delete():
    mem = FakeAsyncMemory(get_all_results=[{"id": "x", "metadata": {"document_id": "d"}}])
    svc = _service(mem)
    await svc.remember_document("u1", items=["a gist"])
    # No document_id -> no idempotent replace path, still fully supported.
    assert mem.get_all_calls == []
    assert mem.delete_by_id_calls == []
    assert "metadata" not in mem.add_calls[0]["kwargs"]


async def test_forget_document_raises_erase_unsupported_without_touching_mem0():
    mem = FakeAsyncMemory(
        get_all_results=[{"id": "a1", "metadata": {"document_id": "docA"}}]
    )
    svc = _service(mem)
    with pytest.raises(MemoryEraseUnsupportedError):
        await svc.forget_document("u1", "docA")
    assert mem.get_all_calls == []
    assert mem.delete_by_id_calls == []
    assert svc._bundle is None


async def test_remember_document_with_id_raises_before_any_mutation():
    # Re-saving a document used to forget the prior generation first for an
    # idempotent replace; that forget can no longer be verified against this
    # backend, so it must raise BEFORE adding anything -- never silently
    # duplicate memories on top of an unverifiable "replace".
    mem = FakeAsyncMemory()
    svc = _service(mem)
    with pytest.raises(MemoryEraseUnsupportedError):
        await svc.remember_document("u1", items=["new gist"], document_id="docA")
    assert mem.get_all_calls == []
    assert mem.delete_by_id_calls == []
    assert mem.add_calls == []
    assert svc._bundle is None


async def test_remember_document_with_id_and_empty_items_returns_zero_without_raising():
    # An empty document has nothing to replace, so this short-circuits to 0
    # (matching prior behavior) without reaching the unsupported-erase path.
    mem = FakeAsyncMemory()
    svc = _service(mem)
    n = await svc.remember_document("u1", items=[], document_id="docA")
    assert n == 0
    assert mem.add_calls == []
    assert svc._bundle is None


# --- delete_memory: fails closed, never touches mem0 -------------------------


async def test_delete_memory_raises_erase_unsupported_without_touching_mem0():
    mem = FakeAsyncMemory(get_results={"m1": {"id": "m1", "user_id": "u1"}})
    svc = _service(mem)

    with pytest.raises(MemoryEraseUnsupportedError):
        await svc.delete_memory("u1", "m1")

    assert mem.get_calls == []
    assert mem.get_all_calls == []
    assert mem.delete_by_id_calls == []
    assert svc._bundle is None
    assert svc.supports_delete is False


# --- lazy build / lifecycle -------------------------------------------------

async def test_factory_built_once_across_calls():
    mem = FakeAsyncMemory(search_results=[])
    svc = _service(mem)
    await svc.recall("u1", "q")
    await svc.remember("u1", "s1", "a durable enough message here")
    await svc.list_memories("u1")  # forget_* no longer builds/uses the bundle
    assert svc._factory_calls["n"] == 1  # type: ignore[attr-defined]


async def test_build_failure_is_retried():
    attempts = {"n": 0}
    good = FakeAsyncMemory(search_results=[])

    def factory() -> Mem0Bundle:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("first build fails")
        return _bundle(good)

    svc = Mem0MemoryService(factory=factory)
    assert await svc.recall("u1", "q") == []  # swallowed first failure
    await svc.recall("u1", "q")  # second attempt builds
    assert attempts["n"] == 2


async def test_warmup_swallows_build_failure():
    def factory() -> Mem0Bundle:
        raise RuntimeError("nope")

    svc = Mem0MemoryService(factory=factory)
    await svc.warmup()  # must not raise


async def test_close_runs_bundle_close_once_and_is_safe_when_unbuilt():
    mem = FakeAsyncMemory(search_results=[])
    svc = _service(mem)
    await svc.recall("u1", "q")
    bundle = svc._bundle
    await svc.close()
    assert bundle._closed["n"] == 1  # type: ignore[attr-defined]
    # Idempotent: a second close (now unbuilt) is a no-op.
    await svc.close()
    assert bundle._closed["n"] == 1  # type: ignore[attr-defined]


async def test_recall_passes_search_threshold():
    mem = FakeAsyncMemory(search_results=[])
    svc = _service(mem, search_threshold=0.42)
    await svc.recall("u1", "q")
    assert mem.search_calls[0]["threshold"] == 0.42


async def test_concurrent_recall_builds_once():
    import asyncio
    import time

    mem = FakeAsyncMemory(search_results=[])
    calls = {"n": 0}

    def factory() -> Mem0Bundle:
        calls["n"] += 1
        time.sleep(0.05)  # widen the window so callers overlap on the build
        return _bundle(mem)

    svc = Mem0MemoryService(factory=factory)
    await asyncio.gather(*(svc.recall("u1", "q") for _ in range(5)))
    assert calls["n"] == 1


async def test_warmup_cancel_does_not_abandon_build():
    import asyncio
    import time

    mem = FakeAsyncMemory(search_results=[])
    calls = {"n": 0}

    def factory() -> Mem0Bundle:
        calls["n"] += 1
        time.sleep(0.1)
        return _bundle(mem)

    svc = Mem0MemoryService(factory=factory)
    # A tight timeout cancels the warmup waiter while the build is in flight.
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await asyncio.wait_for(svc.warmup(), 0.02)
    # The shielded build keeps running and completes exactly once.
    await asyncio.sleep(0.2)
    assert calls["n"] == 1
    assert svc._bundle is not None
    # A later recall reuses the already-built bundle (no second build).
    await svc.recall("u1", "q")
    assert calls["n"] == 1


async def test_close_drains_in_flight_build():
    import asyncio
    import time

    mem = FakeAsyncMemory(search_results=[])
    made: list[Mem0Bundle] = []

    def factory() -> Mem0Bundle:
        time.sleep(0.05)
        b = _bundle(mem)
        made.append(b)
        return b

    svc = Mem0MemoryService(factory=factory)
    task = asyncio.create_task(svc.recall("u1", "q"))
    await asyncio.sleep(0)  # let the build start
    await svc.close()  # must drain the in-flight build, then close its bundle
    await task
    assert len(made) == 1
    assert made[0]._closed["n"] == 1  # type: ignore[attr-defined]
    assert svc._bundle is None


# --- format reuse -----------------------------------------------------------

def test_format_context_reuses_shared_formatter():
    svc = _service(FakeAsyncMemory(), max_injected=2, max_chars_per_item=20)
    block = svc.format_context(
        [MemoryRecord(user_id="u1", text="alpha"), MemoryRecord(user_id="u1", text="beta")]
    )
    assert block is not None and "UNTRUSTED" in block
    assert svc.format_context([]) is None


# --- build_mem0_config ------------------------------------------------------

def test_build_mem0_config_shape():
    pool = object()
    cfg = build_mem0_config(
        endpoint="https://apim.example.net",
        api_key="key-123",
        api_version="2025-04-01-preview",
        llm_deployment="gpt-4.1-mini-dep",
        embed_deployment="tel3-dep",
        embedding_dims=3072,
        collection_name="mem0_memories",
        history_db_path="/tmp/h.db",
        connection_pool=pool,
    )
    llm = cfg["llm"]["config"]
    assert cfg["llm"]["provider"] == "azure_openai"
    assert llm["model"] == "gpt-4.1-mini-dep"
    assert llm["azure_kwargs"]["azure_deployment"] == "gpt-4.1-mini-dep"
    assert llm["azure_kwargs"]["azure_endpoint"] == "https://apim.example.net"
    assert llm["azure_kwargs"]["api_version"] == "2025-04-01-preview"
    assert llm["azure_kwargs"]["api_key"] == "key-123"
    assert llm["azure_kwargs"]["default_headers"] == {"Ocp-Apim-Subscription-Key": "key-123"}

    emb = cfg["embedder"]["config"]
    assert emb["model"] == "tel3-dep"
    assert emb["embedding_dims"] == 3072
    assert emb["azure_kwargs"]["azure_deployment"] == "tel3-dep"

    vs = cfg["vector_store"]["config"]
    assert cfg["vector_store"]["provider"] == "pgvector"
    assert vs["collection_name"] == "mem0_memories"
    assert vs["embedding_model_dims"] == 3072
    assert vs["hnsw"] is False and vs["diskann"] is False
    assert vs["connection_pool"] is pool

    assert cfg["history_db_path"] == "/tmp/h.db"
    assert cfg["version"] == "v1.1"


def test_build_mem0_config_no_key_drops_default_headers():
    cfg = build_mem0_config(
        endpoint="https://apim.example.net",
        api_key=None,
        api_version="2025-04-01-preview",
        llm_deployment="d",
        embed_deployment="e",
        embedding_dims=3072,
        collection_name="c",
        history_db_path="/tmp/h.db",
        connection_pool=object(),
    )
    assert cfg["llm"]["config"]["azure_kwargs"]["default_headers"] is None
    assert cfg["embedder"]["config"]["azure_kwargs"]["api_key"] is None


def test_build_mem0_config_uses_configured_gateway_key_header():
    cfg = build_mem0_config(
        endpoint="https://proxy.example.net",
        api_key="proxy-key",
        api_key_header="S7P-KEY",
        api_version="2025-04-01-preview",
        llm_deployment="llm",
        embed_deployment="embed",
        embedding_dims=3072,
        collection_name="memories",
        history_db_path=":memory:",
        connection_pool=object(),
    )
    expected = {"S7P-KEY": "proxy-key"}
    assert cfg["llm"]["config"]["azure_kwargs"]["default_headers"] == expected
    assert cfg["embedder"]["config"]["azure_kwargs"]["default_headers"] == expected


# --- endpoint normalization -------------------------------------------------

def test_normalize_endpoint_strips_openai_suffix():
    assert (
        normalize_azure_openai_endpoint("https://x.azure-api.net/openai")
        == "https://x.azure-api.net"
    )
    assert (
        normalize_azure_openai_endpoint("https://x.azure-api.net/openai/")
        == "https://x.azure-api.net"
    )
    assert (
        normalize_azure_openai_endpoint("https://x.azure-api.net/OpenAI")
        == "https://x.azure-api.net"
    )
    assert (
        normalize_azure_openai_endpoint("https://x.azure-api.net")
        == "https://x.azure-api.net"
    )


# --- factory selection ------------------------------------------------------

class _Deployment:
    def __init__(self, name: str) -> None:
        self.deploymentName = name


class StubCatalog:
    """Resolves only the model ids present in ``mapping``; others -> None."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def resolve_deployment(self, model_id, **kwargs):
        name = self._mapping.get(model_id)
        return _Deployment(name) if name else None


_FULL_CATALOG = StubCatalog(
    {"text-embedding-3-large": "tel3-dep", "gpt-4.1-mini": "gpt-4.1-mini-dep"}
)


def _mem0_settings(**kw) -> Settings:
    base = dict(
        memory_store=MemoryStoreKind.mem0,
        postgres_host="psql.example.com",
        postgres_user="api-id",
    )
    base.update(kw)
    return Settings(**base)


def test_factory_selects_mem0_service_without_building():
    svc = build_memory_service(
        _mem0_settings(), gateway=object(), catalog=_FULL_CATALOG
    )
    assert isinstance(svc, Mem0MemoryService)
    assert svc.enabled is True
    # Construction must NOT have built the bundle (no DB/mem0 import yet).
    assert svc._bundle is None


def test_factory_mem0_missing_extraction_deployment_is_noop():
    catalog = StubCatalog({"text-embedding-3-large": "tel3-dep"})  # no extraction
    svc = build_memory_service(_mem0_settings(), gateway=object(), catalog=catalog)
    assert isinstance(svc, NoopMemoryService)


def test_factory_mem0_missing_embedding_deployment_is_noop():
    catalog = StubCatalog({"gpt-4.1-mini": "gpt-4.1-mini-dep"})  # no embedding
    svc = build_memory_service(_mem0_settings(), gateway=object(), catalog=catalog)
    assert isinstance(svc, NoopMemoryService)


def test_factory_mem0_without_postgres_is_noop():
    svc = build_memory_service(
        _mem0_settings(postgres_host=None), gateway=object(), catalog=_FULL_CATALOG
    )
    assert isinstance(svc, NoopMemoryService)


# --- config validation ------------------------------------------------------

def test_validate_runtime_mem0_requires_postgres_host():
    with pytest.raises(RuntimeError, match="POSTGRES_HOST"):
        Settings(
            memory_store=MemoryStoreKind.mem0, postgres_user="api-id"
        ).validate_runtime()


def test_validate_runtime_mem0_requires_postgres_user():
    with pytest.raises(RuntimeError, match="POSTGRES_USER"):
        Settings(
            memory_store=MemoryStoreKind.mem0, postgres_host="psql.example.com"
        ).validate_runtime()


def test_validate_runtime_mem0_with_postgres_ok():
    Settings(
        memory_store=MemoryStoreKind.mem0,
        postgres_host="psql.example.com",
        postgres_user="api-id",
    ).validate_runtime()  # must not raise
