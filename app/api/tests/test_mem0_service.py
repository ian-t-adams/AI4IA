"""Unit tests for the real-mem0 backend.

A fake ``AsyncMemory`` (injected through the ``factory`` callable) lets these run
without the ``mem0`` library or any database, covering: the result mapping,
best-effort swallowing, the scoping asymmetry (filters dict on search/get_all vs
top-level kwargs on add), forget counts (including multi-pass convergence and
failure propagation -- forget never delegates to mem0's own swallowed/capped
delete_all), lazy single build + retry, the config-dict shape, endpoint
normalization, and factory backend selection.
"""
from __future__ import annotations

import pytest

from ai4ia_api.config import MemoryStoreKind, Settings
from ai4ia_api.memory.factory import build_memory_service
from ai4ia_api.memory.mem0_service import (
    _FORGET_LIST_CAP,
    Mem0Bundle,
    Mem0MemoryService,
    MemoryScanIncompleteError,
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


# --- forget -----------------------------------------------------------------

async def test_forget_user_counts_then_deletes():
    mem = FakeAsyncMemory(get_all_results=[{"id": 1}, {"id": 2}, {"id": 3}])
    svc = _service(mem)
    n = await svc.forget_user("u1")
    assert n == 3
    assert mem.get_all_calls[0]["filters"] == {"user_id": "u1"}
    # No longer delegates to mem0's own delete_all -- explicit per-id delete.
    assert mem.delete_by_id_calls == [
        {"memory_id": 1},
        {"memory_id": 2},
        {"memory_id": 3},
    ]


async def test_forget_session_scopes_by_run_id():
    mem = FakeAsyncMemory(get_all_results=[{"id": 1}])
    svc = _service(mem)
    n = await svc.forget_session("u1", "s9")
    assert n == 1
    assert mem.get_all_calls[0]["filters"] == {"user_id": "u1", "run_id": "s9"}
    assert mem.delete_by_id_calls == [{"memory_id": 1}]


async def test_forget_propagates_errors():
    class Boom(FakeAsyncMemory):
        async def get_all(self, *a, **k):
            raise RuntimeError("store down")

    svc = _service(Boom())
    with pytest.raises(RuntimeError):
        await svc.forget_user("u1")


# --- round 12 HIGH acceptance finding: forget_user/forget_session used to
# delegate to mem0's own delete_all(), which internally lists via
# vector_store.list(filters) with NO limit at all -- silently falling back to
# the pgvector store's own default top_k of 100, wholly unrelated to our
# _FORGET_LIST_CAP -- then deletes via asyncio.gather(return_exceptions=True)
# and unconditionally returns a success message even when some deletes
# failed (only a warning is logged). A "successful" forget_user on an account
# with >100 memories therefore silently left most of them intact and
# recallable, and any individual delete failure was invisible to the caller.
# Fixed by _forget_by_filter: explicit list-then-delete-by-id in verified,
# re-queried passes, with any failure propagating rather than being
# swallowed, and MemoryScanIncompleteError raised if the scope never
# converges to empty. ---


async def test_forget_user_deletes_more_than_a_single_backend_page():
    # More than mem0's own internal delete_all() page size (100, from
    # pgvector's default top_k) and more than one _FORGET_LIST_CAP page size
    # apart -- proving the whole scope converges across multiple list+delete
    # passes rather than being silently capped like the old delete_all path.
    total = _FORGET_LIST_CAP + 250
    mem = FakeAsyncMemory(
        get_all_results=[{"id": f"m{i}"} for i in range(total)]
    )
    svc = _service(mem)
    n = await svc.forget_user("u1")
    assert n == total
    assert len(mem.delete_by_id_calls) == total
    # Converged: nothing left to list for this user.
    assert mem._get_all_results == []
    # More than one page was required to enumerate the full scope.
    assert len(mem.get_all_calls) >= 2


async def test_forget_session_propagates_individual_delete_failures():
    # One of several matching memories fails to delete -- must raise, not
    # report a "successful" count that leaves it (and anything after it in
    # the pass) behind, unlike mem0's delete_all which only logs a warning.
    class FlakyDelete(FakeAsyncMemory):
        async def delete(self, memory_id=None):
            if memory_id == "bad":
                raise RuntimeError("delete failed")
            return await super().delete(memory_id=memory_id)

    mem = FlakyDelete(
        get_all_results=[{"id": "ok1"}, {"id": "bad"}, {"id": "ok2"}]
    )
    svc = _service(mem)
    with pytest.raises(RuntimeError, match="delete failed"):
        await svc.forget_session("u1", "s1")
    # The failure surfaced instead of being swallowed; whatever succeeded
    # before the failure is reflected in the delete calls (no double-counting
    # or silently-reported success).
    assert {c["memory_id"] for c in mem.delete_by_id_calls} <= {"ok1", "bad", "ok2"}


async def test_forget_user_raises_on_no_progress_between_passes():
    # Every pass sees the same number of "remaining" memories (e.g. writes
    # landing in the same scope as fast as they're deleted, or a backend
    # whose delete() silently no-ops) -- must fail closed rather than loop
    # forever or report a false success.
    class NoProgress(FakeAsyncMemory):
        async def delete(self, memory_id=None):
            self.delete_by_id_calls.append({"memory_id": memory_id})
            # Deliberately do NOT remove from _get_all_results, simulating a
            # delete that "succeeds" without taking effect.
            return {"message": "ok"}

    mem = NoProgress(get_all_results=[{"id": 1}, {"id": 2}])
    svc = _service(mem)
    with pytest.raises(MemoryScanIncompleteError):
        await svc.forget_user("u1")


async def test_forget_user_raises_when_passes_exhausted_with_remainder():
    # A scope that keeps shrinking (so "no progress" never fires) but never
    # quite reaches empty within the pass budget must still fail closed
    # rather than return a count while memories remain.
    class OneShort(FakeAsyncMemory):
        async def delete(self, memory_id=None):
            self.delete_by_id_calls.append({"memory_id": memory_id})
            # Remove the target, but always leave one extra straggler behind
            # so the listing never becomes empty.
            self._get_all_results = [
                r for r in self._get_all_results if r.get("id") != memory_id
            ]
            if not self._get_all_results:
                self._get_all_results = [{"id": "straggler"}]
            return {"message": "ok"}

    mem = OneShort(get_all_results=[{"id": f"m{i}"} for i in range(5)])
    svc = _service(mem)
    with pytest.raises(MemoryScanIncompleteError):
        await svc.forget_user("u1")


# --- 11E-3: document-scoped save (idempotent) + forget-by-document ---

async def test_remember_document_tags_metadata_and_infer_false():
    mem = FakeAsyncMemory()
    svc = _service(mem)
    n = await svc.remember_document(
        "u1", items=["excerpt one", "excerpt two"], document_id="docA"
    )
    assert n == 2
    # Each excerpt is stored verbatim (infer=False) and tagged with the document.
    for call in mem.add_calls:
        assert call["kwargs"]["infer"] is False
        assert call["kwargs"]["user_id"] == "u1"
        assert call["kwargs"]["metadata"] == {"document_id": "docA"}


async def test_remember_document_idempotent_deletes_prior_generation():
    # A prior save for docA exists; re-saving must remove it first, then add anew.
    mem = FakeAsyncMemory(
        get_all_results=[
            {"id": "old1", "metadata": {"document_id": "docA"}},
            {"id": "other", "metadata": {"document_id": "docB"}},
            {"id": "chat", "metadata": {}},
        ]
    )
    svc = _service(mem)
    await svc.remember_document("u1", items=["new gist"], document_id="docA")
    # Only docA's prior memory is deleted by id (docB + chat untouched).
    assert mem.delete_by_id_calls == [{"memory_id": "old1"}]
    # Then the fresh excerpt is added.
    assert mem.add_calls[0]["kwargs"]["metadata"] == {"document_id": "docA"}


async def test_remember_document_without_id_does_not_list_or_delete():
    mem = FakeAsyncMemory(get_all_results=[{"id": "x", "metadata": {"document_id": "d"}}])
    svc = _service(mem)
    await svc.remember_document("u1", items=["a gist"])
    # No document_id -> no idempotent replace path.
    assert mem.get_all_calls == []
    assert mem.delete_by_id_calls == []
    assert "metadata" not in mem.add_calls[0]["kwargs"]


async def test_forget_document_deletes_only_matching_metadata():
    mem = FakeAsyncMemory(
        get_all_results=[
            {"id": "a1", "metadata": {"document_id": "docA"}},
            {"id": "a2", "metadata": {"document_id": "docA"}},
            {"id": "b1", "metadata": {"document_id": "docB"}},
            {"id": "c1", "metadata": {}},
            {"id": None, "metadata": {"document_id": "docA"}},  # no id -> skipped
        ]
    )
    svc = _service(mem)
    n = await svc.forget_document("u1", "docA")
    assert n == 2
    assert mem.get_all_calls[0]["filters"] == {"user_id": "u1", "document_id": "docA"}
    assert mem.delete_by_id_calls == [{"memory_id": "a1"}, {"memory_id": "a2"}]


async def test_forget_document_propagates_errors():
    class Boom(FakeAsyncMemory):
        async def get_all(self, *a, **k):
            raise RuntimeError("store down")

    svc = _service(Boom())
    with pytest.raises(RuntimeError):
        await svc.forget_document("u1", "docA")


# --- round 6 MEDIUM acceptance finding: mem0 forget/replace must not silently
# under-scan. mem0's get_all() has no pagination cursor -- only a top_k cap --
# so a *document* with >= _FORGET_LIST_CAP tagged memories could have stray
# ones sitting beyond the enumerated page. Reporting "N forgotten" in that
# case previously left the document's memories fully recallable while every
# caller believed the erase (or idempotent re-save's pre-delete) had fully
# succeeded. ---
#
# --- round 11 MEDIUM acceptance finding: the listing was scoped by user_id
# only, so the cap fired based on a user's *total* memory count across every
# document, not the target document's own count -- an active user with many
# small documents could trip MemoryScanIncompleteError and be unable to
# forget/re-save *any* single document, even though none of their documents
# individually held anywhere near _FORGET_LIST_CAP memories. The listing now
# also filters by document_id server-side (mem0's pgvector store matches
# arbitrary metadata keys, not just user_id/agent_id/run_id), so the cap is
# scoped to the target document alone. ---


async def test_forget_document_raises_when_listing_hits_the_cap():
    # Exactly _FORGET_LIST_CAP rows for this one document come back: the
    # document-scoped listing may have been truncated by mem0, so
    # completeness cannot be verified.
    mem = FakeAsyncMemory(
        get_all_results=[
            {"id": f"m{i}", "metadata": {"document_id": "docA"}}
            for i in range(_FORGET_LIST_CAP)
        ]
    )
    svc = _service(mem)
    with pytest.raises(MemoryScanIncompleteError):
        await svc.forget_document("u1", "docA")
    # Fails closed: nothing was deleted from a possibly-incomplete view.
    assert mem.delete_by_id_calls == []


async def test_forget_document_below_cap_boundary_still_succeeds():
    # One row under the cap: get_all's top_k was not exhausted, so the
    # listing is provably complete and normal deletion proceeds.
    mem = FakeAsyncMemory(
        get_all_results=[
            {"id": f"m{i}", "metadata": {"document_id": "docA"}}
            for i in range(_FORGET_LIST_CAP - 1)
        ]
    )
    svc = _service(mem)
    n = await svc.forget_document("u1", "docA")
    assert n == _FORGET_LIST_CAP - 1
    assert len(mem.delete_by_id_calls) == _FORGET_LIST_CAP - 1


async def test_forget_document_ignores_unrelated_memories_beyond_the_cap():
    # Regression for the round-11 finding: 1,000 memories belong to OTHER
    # documents (well past _FORGET_LIST_CAP on their own) plus 2 belong to
    # the target document. Because the listing is scoped by document_id
    # server-side, only the 2 matching rows are ever seen/counted, so the
    # cap never fires and both are deleted -- proving the bound is per
    # document, not per user.
    unrelated = [
        {"id": f"other{i}", "metadata": {"document_id": "docOther"}}
        for i in range(_FORGET_LIST_CAP)
    ]
    target = [
        {"id": "a1", "metadata": {"document_id": "docA"}},
        {"id": "a2", "metadata": {"document_id": "docA"}},
    ]
    mem = FakeAsyncMemory(get_all_results=unrelated + target)
    svc = _service(mem)
    n = await svc.forget_document("u1", "docA")
    assert n == 2
    assert mem.get_all_calls[0]["filters"] == {"user_id": "u1", "document_id": "docA"}
    assert mem.delete_by_id_calls == [{"memory_id": "a1"}, {"memory_id": "a2"}]


async def test_forget_document_local_recheck_still_filters_a_permissive_backend():
    # Defense-in-depth: if a backend/fake ignores the document_id filter
    # (unlike mem0's real pgvector store) and returns every memory
    # regardless of scoping, the existing local metadata re-check inside
    # _forget_document must still ensure only matching rows get deleted.
    class PermissiveFakeAsyncMemory(FakeAsyncMemory):
        async def get_all(self, *, filters=None, top_k=20, **kwargs):
            self.get_all_calls.append({"filters": filters, "top_k": top_k})
            return {"results": list(self._get_all_results)}

    mem = PermissiveFakeAsyncMemory(
        get_all_results=[
            {"id": "a1", "metadata": {"document_id": "docA"}},
            {"id": "b1", "metadata": {"document_id": "docB"}},
            {"id": "c1", "metadata": {}},
        ]
    )
    svc = _service(mem)
    n = await svc.forget_document("u1", "docA")
    assert n == 1
    assert mem.delete_by_id_calls == [{"memory_id": "a1"}]


async def test_remember_document_raises_when_listing_hits_the_cap():
    # The idempotent pre-delete shares _forget_document, so a re-save must
    # also refuse to proceed rather than add a new generation on top of an
    # erase that could not verify it removed every prior one.
    mem = FakeAsyncMemory(
        get_all_results=[
            {"id": f"m{i}", "metadata": {"document_id": "docA"}}
            for i in range(_FORGET_LIST_CAP)
        ]
    )
    svc = _service(mem)
    with pytest.raises(MemoryScanIncompleteError):
        await svc.remember_document("u1", items=["new gist"], document_id="docA")
    # Fails before adding the new generation -- no partial duplicate state.
    assert mem.add_calls == []


# --- delete_memory ------------------------------------------------------

async def test_delete_memory_deletes_owned_memory_via_direct_lookup():
    mem = FakeAsyncMemory(get_results={"m1": {"id": "m1", "user_id": "u1"}})
    svc = _service(mem)
    assert await svc.delete_memory("u1", "m1") is True
    assert mem.get_calls == ["m1"]
    assert mem.delete_by_id_calls == [{"memory_id": "m1"}]
    # A direct get(id) lookup replaces the old list-then-scan approach, so it
    # never lists the user's other memories.
    assert mem.get_all_calls == []


async def test_delete_memory_returns_false_when_memory_missing():
    mem = FakeAsyncMemory(get_results={})
    svc = _service(mem)
    assert await svc.delete_memory("u1", "missing") is False
    assert mem.delete_by_id_calls == []


async def test_delete_memory_returns_false_for_other_users_memory():
    # Ownership is enforced from the fetched item's user_id, not the caller's
    # say-so: a memory that exists but belongs to someone else must not delete.
    mem = FakeAsyncMemory(get_results={"m1": {"id": "m1", "user_id": "attacker"}})
    svc = _service(mem)
    assert await svc.delete_memory("u1", "m1") is False
    assert mem.delete_by_id_calls == []


async def test_delete_memory_not_bounded_by_forget_list_cap():
    """Regression: the old implementation listed up to _FORGET_LIST_CAP memories
    and reported "not found" for anything beyond that enumeration bound. A
    direct get(id) lookup has no such scale-dependent blind spot."""
    mem = FakeAsyncMemory(get_results={"beyond-cap": {"id": "beyond-cap", "user_id": "u1"}})
    svc = _service(mem)
    assert await svc.delete_memory("u1", "beyond-cap") is True


# --- lazy build / lifecycle -------------------------------------------------

async def test_factory_built_once_across_calls():
    mem = FakeAsyncMemory(search_results=[])
    svc = _service(mem)
    await svc.recall("u1", "q")
    await svc.remember("u1", "s1", "a durable enough message here")
    await svc.forget_user("u1")
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
