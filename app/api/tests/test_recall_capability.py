"""The ``recall_memory`` synthetic capability (WS2 part D).

Proves the tool is user-scoped (closure-bound, never spoofable from args),
nonce-fenced, budget-bounded, fail-soft, and registered as a user-selectable
synthetic tool. A handler can therefore never read another user's memory.
"""
from __future__ import annotations

from ai4ia_api.agents.tool_exec import (
    SELECTABLE_SYNTHETIC_TOOL_NAMES,
    ToolContext,
    attachable_tool_names,
    build_tools,
)
from ai4ia_api.memory.models import MemoryRecord
from ai4ia_api.memory.recall_capability import (
    MAX_RECALLS_PER_TURN,
    RECALL_TOOL_NAME,
    build_recall_capability,
)


class _FakeMemory:
    enabled = True

    def __init__(self, records=None, raises=False) -> None:
        self._records = records or []
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    async def recall(self, user_id, query):
        self.calls.append((user_id, query))
        if self._raises:
            raise RuntimeError("boom")
        return list(self._records)


def _rec(text, *, session_id=None):
    return MemoryRecord(user_id="alice", text=text, session_id=session_id)


def _build(memory, *, user_id="alice", nonce="NONCE", session_id="s1"):
    tools, handlers = build_recall_capability(
        memory=memory, user_id=user_id, nonce=nonce, session_id=session_id
    )
    return tools, handlers[RECALL_TOOL_NAME]


# --- registration -------------------------------------------------------------


def test_recall_is_selectable_synthetic_tool():
    assert RECALL_TOOL_NAME in SELECTABLE_SYNTHETIC_TOOL_NAMES
    registry, executor = build_tools()
    assert RECALL_TOOL_NAME in attachable_tool_names(registry, executor)


def test_schema_shape():
    tools, _ = _build(_FakeMemory())
    fn = tools[0]["function"]
    assert fn["name"] == RECALL_TOOL_NAME
    assert fn["parameters"]["required"] == ["query"]
    assert set(fn["parameters"]["properties"]) == {"query", "scope"}


# --- user scoping (cannot cross users) ----------------------------------------


async def test_recall_uses_closure_user_id_not_args():
    mem = _FakeMemory(records=[_rec("alice likes python")])
    _, handler = _build(mem, user_id="alice")
    # Even if a caller smuggles a user_id into args, it is ignored.
    out = await handler({"query": "language", "user_id": "mallory"}, ToolContext())
    assert mem.calls == [("alice", "language")]
    assert "alice likes python" in out["results"]


async def test_results_are_nonce_fenced():
    mem = _FakeMemory(records=[_rec("a durable fact")])
    _, handler = _build(mem, nonce="ZZZ")
    out = await handler({"query": "fact"}, ToolContext())
    assert out["results"].startswith("BEGIN MEMORY ZZZ")
    assert out["results"].rstrip().endswith("END MEMORY ZZZ")
    assert "untrusted" in out["note"].lower()


# --- scope filtering ----------------------------------------------------------


async def test_session_scope_filters_to_current_session():
    mem = _FakeMemory(records=[
        _rec("from this chat", session_id="s1"),
        _rec("from another chat", session_id="s2"),
    ])
    _, handler = _build(mem, session_id="s1")
    out = await handler({"query": "x", "scope": "session"}, ToolContext())
    assert "from this chat" in out["results"]
    assert "from another chat" not in out["results"]


async def test_all_scope_returns_cross_session_memories():
    mem = _FakeMemory(records=[
        _rec("from this chat", session_id="s1"),
        _rec("from another chat", session_id="s2"),
    ])
    _, handler = _build(mem, session_id="s1")
    out = await handler({"query": "x", "scope": "all"}, ToolContext())
    assert "from this chat" in out["results"]
    assert "from another chat" in out["results"]


# --- budget, validation, fail-soft --------------------------------------------


async def test_budget_exhausts_after_cap():
    mem = _FakeMemory(records=[_rec("fact")])
    _, handler = _build(mem)
    for _ in range(MAX_RECALLS_PER_TURN):
        assert "error" not in await handler({"query": "q"}, ToolContext())
    blocked = await handler({"query": "q"}, ToolContext())
    assert "budget" in blocked["error"]
    # The blocked call did not reach the store.
    assert len(mem.calls) == MAX_RECALLS_PER_TURN


async def test_empty_query_rejected():
    mem = _FakeMemory()
    _, handler = _build(mem)
    out = await handler({"query": "   "}, ToolContext())
    assert "error" in out
    assert not mem.calls  # never hit the store


async def test_fail_soft_on_store_error():
    mem = _FakeMemory(raises=True)
    _, handler = _build(mem)
    out = await handler({"query": "q"}, ToolContext())
    # Degrades to "no memories" rather than raising into the turn.
    assert out["results"] == ""
    assert out["count"] == 0


async def test_no_relevant_memories_returns_empty():
    mem = _FakeMemory(records=[])
    _, handler = _build(mem)
    out = await handler({"query": "q"}, ToolContext())
    assert out["results"] == ""
    assert out["count"] == 0


async def test_results_are_capped():
    mem = _FakeMemory(records=[_rec(f"fact {i}") for i in range(20)])
    _, handler = _build(mem)
    out = await handler({"query": "q"}, ToolContext())
    # Capped to the per-turn item ceiling.
    assert out["count"] <= 5
