"""Workflow steps must run with the same synthetic capabilities chat offers.

Before this, ``run_workflow_step`` called ``run_agent_turn`` with **no**
``extra_tools``/``extra_handlers`` at all, so every step ran with only the two
registry built-ins (``calculator``, ``get_current_time``) regardless of what its
agent declared. Nothing errored — the model simply answered that it could not
read documents, search the web, or remember anything, and the run persisted that
answer as a success. These tests pin the wiring end to end so that silence
cannot come back.
"""
from __future__ import annotations

from typing import Any

from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec
from ai4ia_api.agents.capabilities import (
    build_shared_capabilities,
    capability_builder_for_state,
)
from ai4ia_api.agents.tool_exec import (
    SELECTABLE_SYNTHETIC_TOOL_NAMES,
    ToolContext,
    build_tools,
)
from ai4ia_api.memory.recall_capability import RECALL_TOOL_NAME
from ai4ia_api.memory.remember_capability import (
    MAX_REMEMBERS_PER_TURN,
    MAX_TEXT_LEN,
    REMEMBER_TOOL_NAME,
    build_remember_capability,
)
from ai4ia_api.workflows.models import WorkflowStep
from ai4ia_api.workflows.runner import run_workflow_step

_USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


class _CapturingGateway:
    """Records the tool schema each turn was actually offered."""

    def __init__(self) -> None:
        self.tool_names: list[list[str]] = []

    async def complete(
        self, *, deployment, messages, params=None, correlation_id=None, api="chat"
    ):
        offered = [
            t["function"]["name"] for t in (params or {}).get("tools", []) or []
        ]
        self.tool_names.append(offered)
        return {"choices": [{"message": {"content": "done"}}], "usage": _USAGE}

    async def stream(self, **_kwargs):  # pragma: no cover - runner never streams
        raise AssertionError("runner must not stream")


class _FakeMemory:
    """Minimal MemoryServiceProtocol stand-in that records writes."""

    def __init__(self, *, enabled: bool = True, stores: bool = True) -> None:
        self.enabled = enabled
        self._stores = stores
        self.writes: list[tuple[str, str | None, str]] = []

    async def recall(self, *_args, **_kwargs):  # pragma: no cover - unused here
        return []

    async def remember(self, user_id: str, session_id: str | None, text: str) -> bool:
        self.writes.append((user_id, session_id, text))
        return self._stores


class _FakeWebSearch:
    def build_capability(self, *, user_id: str, session_id: str, nonce: str):
        async def _handler(_args: dict[str, Any], _ctx: ToolContext) -> dict[str, Any]:
            return {"results": []}

        schema = {
            "type": "function",
            "function": {"name": "web_search", "parameters": {"type": "object"}},
        }
        return [schema], {"web_search": _handler}


def _agent(name: str, tools: list[str] | None = None) -> AgentSpec:
    return AgentSpec(
        name=name,
        displayName=name,
        description="",
        systemPrompt=f"You are {name}.",
        tools=tools or [],
    )


async def _run_step(*, agent: AgentSpec, capabilities=None):
    gw = _CapturingGateway()
    registry, executor = build_tools()
    outcome = await run_workflow_step(
        WorkflowStep(agent=agent.name, instruction="Do {input}"),
        index=0,
        workflow_name="flow",
        run_input="hello",
        previous="",
        composed=AgentCatalog(agents=[agent]),
        deployment="dep-1",
        gateway=gw,
        registry=registry,
        executor=executor,
        capabilities=capabilities,
        correlation_id="cid",
    )
    return outcome, gw


# --- The defect itself ---------------------------------------------------------


async def test_a_step_is_offered_the_injected_capabilities():
    memory = _FakeMemory()
    state = type(
        "S", (), {"document_retrieval": None, "web_search": _FakeWebSearch(), "memory": memory}
    )()
    builder = capability_builder_for_state(
        state, user_id="u1", session_id="s1", email="u@example.com"
    )
    agent = _agent("a1", tools=[REMEMBER_TOOL_NAME, RECALL_TOOL_NAME])

    outcome, gw = await _run_step(agent=agent, capabilities=builder)

    assert outcome.result.ok
    offered = gw.tool_names[0]
    assert "web_search" in offered
    assert REMEMBER_TOOL_NAME in offered
    assert RECALL_TOOL_NAME in offered


async def test_without_a_builder_a_step_gets_only_registry_tools():
    """Non-vacuity control for the test above.

    Proves the assertions there are observing the *builder*, not a tool surface a
    step would have had anyway — this is exactly the broken state that shipped.
    """
    agent = _agent("a1", tools=[REMEMBER_TOOL_NAME, RECALL_TOOL_NAME])

    _outcome, gw = await _run_step(agent=agent, capabilities=None)

    offered = gw.tool_names[0]
    assert REMEMBER_TOOL_NAME not in offered
    assert RECALL_TOOL_NAME not in offered
    assert "web_search" not in offered


async def test_a_failing_capability_builder_degrades_instead_of_failing_the_run():
    def _boom(_tools):
        raise RuntimeError("builder exploded")

    agent = _agent("a1", tools=[])
    outcome, gw = await _run_step(agent=agent, capabilities=_boom)

    assert outcome.result.ok, "a capability failure must never fail the step"
    assert outcome.fatal is False
    assert gw.tool_names[0] == []


# --- Gating -------------------------------------------------------------------


def test_memory_tools_are_only_offered_when_attached():
    memory = _FakeMemory()
    with_attach = build_shared_capabilities(
        attached_tool_names=[REMEMBER_TOOL_NAME],
        user_id="u1",
        nonce="n",
        memory=memory,
    )
    without = build_shared_capabilities(
        attached_tool_names=[], user_id="u1", nonce="n", memory=memory
    )

    assert REMEMBER_TOOL_NAME in with_attach.handlers
    assert without.handlers == {}


def test_an_attached_memory_tool_with_memory_off_is_reported_not_silently_dropped():
    built = build_shared_capabilities(
        attached_tool_names=[REMEMBER_TOOL_NAME, RECALL_TOOL_NAME],
        user_id="u1",
        nonce="n",
        memory=_FakeMemory(enabled=False),
    )

    assert built.handlers == {}
    assert built.unavailable[REMEMBER_TOOL_NAME] == "memory is not enabled"
    assert built.unavailable[RECALL_TOOL_NAME] == "memory is not enabled"


def test_web_search_needs_a_session_to_meter_against():
    built = build_shared_capabilities(
        attached_tool_names=[],
        user_id="u1",
        nonce="n",
        session_id=None,
        web_search=_FakeWebSearch(),
    )

    assert built.handlers == {}
    assert "web_search" in built.unavailable


def test_remember_memory_is_user_attachable():
    assert REMEMBER_TOOL_NAME in SELECTABLE_SYNTHETIC_TOOL_NAMES


# --- Honest write reporting ----------------------------------------------------


async def _call_remember(memory: _FakeMemory, text: str, *, times: int = 1):
    _tools, handlers = build_remember_capability(
        memory=memory, user_id="u1", session_id="s1"
    )
    handler = handlers[REMEMBER_TOOL_NAME]
    out: dict[str, Any] = {}
    for _ in range(times):
        out = await handler({"text": text}, ToolContext())
    return out


async def test_a_stored_write_reports_saved():
    memory = _FakeMemory(stores=True)
    out = await _call_remember(memory, "The launch is in March.")
    assert out == {"saved": True, "text": "The launch is in March."}
    assert memory.writes == [("u1", "s1", "The launch is in March.")]


async def test_a_skipped_write_is_not_reported_as_saved():
    """A 'saved' that did not happen is a lie the model repeats to the user."""
    memory = _FakeMemory(stores=False)
    out = await _call_remember(memory, "Nothing new here.")
    assert out["saved"] is False
    assert "note" in out and "not an error" in out["note"]


async def test_a_raising_memory_service_reports_unavailable_rather_than_saved():
    class _Boom(_FakeMemory):
        async def remember(self, *_args, **_kwargs) -> bool:
            raise RuntimeError("cosmos down")

    out = await _call_remember(_Boom(), "Some durable fact.")
    assert out["saved"] is False
    assert "unavailable" in out["error"]


async def test_oversized_text_is_refused_without_touching_the_store():
    memory = _FakeMemory()
    out = await _call_remember(memory, "x" * (MAX_TEXT_LEN + 1))
    assert out["saved"] is False
    assert memory.writes == []


async def test_the_per_turn_write_budget_is_enforced():
    memory = _FakeMemory()
    out = await _call_remember(memory, "a fact", times=MAX_REMEMBERS_PER_TURN + 1)
    assert out["saved"] is False
    assert "budget" in out["error"]
    assert len(memory.writes) == MAX_REMEMBERS_PER_TURN


async def test_the_write_is_bound_to_the_closure_user_not_a_tool_argument():
    memory = _FakeMemory()
    _tools, handlers = build_remember_capability(
        memory=memory, user_id="owner", session_id="s1"
    )
    # A model that tries to name a different user must be ignored, not obeyed.
    await handlers[REMEMBER_TOOL_NAME](
        {"text": "a fact", "user_id": "victim", "userId": "victim"}, ToolContext()
    )
    assert memory.writes == [("owner", "s1", "a fact")]


# --- Both execution modes, one surface ----------------------------------------


def test_both_workflow_execution_paths_inject_capabilities():
    """The in-request runner and the durable activity must not drift.

    A step's tool surface has to be identical whichever mode ran it. Asserted at
    the source level because the alternative — one path quietly omitting the
    argument — produces no error at all, just a model that says it cannot do the
    job (exactly how this shipped).
    """
    import inspect

    from ai4ia_api.routers import workflows as workflows_router
    from ai4ia_api.workflows import durable as durable_module

    for module in (workflows_router, durable_module):
        source = inspect.getsource(module)
        assert "capability_builder_for_state(" in source, (
            f"{module.__name__} must build capabilities for its workflow steps"
        )
        assert "capabilities=capability_builder_for_state(" in source, (
            f"{module.__name__} must pass them to the runner"
        )


def test_document_scoping_is_frozen_into_the_durable_payload():
    """Re-reading the session inside the activity would let a mid-run edit widen
    or narrow an in-flight run's document access."""
    from ai4ia_api.workflows.durable import build_orchestration_payload
    from ai4ia_api.workflows.models import Workflow

    workflow = Workflow(
        id="flow",
        name="flow",
        displayName="Flow",
        userId="u1",
        steps=[WorkflowStep(agent="a1", instruction="Do {input}")],
    )
    payload = build_orchestration_payload(
        workflow,
        user_id="u1",
        session_id="s1",
        run_input="hi",
        model_id="m",
        deployment="dep-1",
        correlation_id="cid",
        email="u@example.com",
        library_document_ids=["doc-1"],
    )

    assert payload["context"]["email"] == "u@example.com"
    assert payload["context"]["libraryDocumentIds"] == ["doc-1"]
