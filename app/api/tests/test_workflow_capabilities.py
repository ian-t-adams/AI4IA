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
from ai4ia_api.catalog import DeploymentOption
from ai4ia_api.agents.capabilities import (
    build_shared_capabilities,
    capability_builder_for_state,
)
from ai4ia_api.agents.tool_exec import (
    CHAT_ONLY_SYNTHETIC_TOOL_NAMES,
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
from ai4ia_api.memory.service import MemoryWriteOutcome
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

    def __init__(
        self, *, enabled: bool = True, outcome: MemoryWriteOutcome = "saved"
    ) -> None:
        self.enabled = enabled
        self._outcome: MemoryWriteOutcome = outcome
        self.writes: list[tuple[str, str | None, str]] = []

    async def recall(self, *_args, **_kwargs):  # pragma: no cover - unused here
        return []

    async def remember(
        self, user_id: str, session_id: str | None, text: str
    ) -> MemoryWriteOutcome:
        self.writes.append((user_id, session_id, text))
        return self._outcome


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


async def _run_step(*, agent: AgentSpec, capabilities=None, extra_tools=None):
    gw = _CapturingGateway()
    registry, executor = build_tools()
    outcome = await run_workflow_step(
        WorkflowStep(
            agent=agent.name, instruction="Do {input}", extraTools=extra_tools or []
        ),
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


# --- Per-step extra tools ------------------------------------------------------
#
# The curated agents ship with fixed tool lists that a user cannot edit — `general`
# declares only `get_current_time`. Because the memory tools are attach-gated, a
# workflow step targeting a curated agent could never save a memory: the model was
# offered no such tool, so it wrote a confident summary claiming it had recorded
# everything while the user's memory store stayed empty. Observed in production
# against a real workflow before these tests existed.


async def test_a_step_can_add_a_tool_its_curated_agent_does_not_declare():
    memory = _FakeMemory()
    state = type("S", (), {"document_retrieval": None, "web_search": None, "memory": memory})()
    builder = capability_builder_for_state(state, user_id="u1", session_id="s1")
    curated = _agent("general", tools=["get_current_time"])

    outcome, gw = await _run_step(
        agent=curated, capabilities=builder, extra_tools=[REMEMBER_TOOL_NAME]
    )

    assert outcome.result.ok
    offered = gw.tool_names[0]
    assert REMEMBER_TOOL_NAME in offered
    # The agent's own tool survives: extraTools adds, it does not replace.
    assert "get_current_time" in offered


async def test_the_same_step_without_extra_tools_is_not_offered_the_tool():
    """Non-vacuity control: proves the assertion above observes ``extraTools``.

    Without this, the test would still pass if the memory tool were offered to
    every step unconditionally — which would be a different bug, not a fix.
    """
    memory = _FakeMemory()
    state = type("S", (), {"document_retrieval": None, "web_search": None, "memory": memory})()
    builder = capability_builder_for_state(state, user_id="u1", session_id="s1")
    curated = _agent("general", tools=["get_current_time"])

    _outcome, gw = await _run_step(agent=curated, capabilities=builder, extra_tools=[])

    assert REMEMBER_TOOL_NAME not in gw.tool_names[0]


async def test_a_step_can_add_a_registry_tool_too():
    """Pins ``tool_names``, not just the capability builder.

    ``remember_memory`` is *synthetic* — it reaches the model through
    ``extra_tools``, which ``run_agent_turn`` appends wholesale. A registry tool
    is resolved from ``tool_names`` instead, so only this test proves the second
    half of the fix.
    """
    agent = _agent("a1", tools=[])

    _outcome, gw = await _run_step(agent=agent, extra_tools=["calculator"])

    assert "calculator" in gw.tool_names[0]


async def test_a_tool_the_agent_already_declares_is_not_offered_twice():
    memory = _FakeMemory()
    state = type("S", (), {"document_retrieval": None, "web_search": None, "memory": memory})()
    builder = capability_builder_for_state(state, user_id="u1", session_id="s1")
    agent = _agent("a1", tools=[REMEMBER_TOOL_NAME])

    _outcome, gw = await _run_step(
        agent=agent, capabilities=builder, extra_tools=[REMEMBER_TOOL_NAME]
    )

    offered = gw.tool_names[0]
    assert offered.count(REMEMBER_TOOL_NAME) == 1


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
    memory = _FakeMemory(outcome="saved")
    out = await _call_remember(memory, "The launch is in March.")
    assert out == {"saved": True, "text": "The launch is in March."}
    assert memory.writes == [("u1", "s1", "The launch is in March.")]


async def test_a_skipped_write_is_not_reported_as_saved():
    """A 'saved' that did not happen is a lie the model repeats to the user."""
    memory = _FakeMemory(outcome="noop")
    out = await _call_remember(memory, "Nothing new here.")
    assert out["saved"] is False
    assert "note" in out and "not an error" in out["note"]


async def test_an_unavailable_write_is_reported_as_an_error_not_a_noop():
    """The critical distinction: an outage must NOT read as 'already covered'.

    The real services never raise — they swallow failures internally — so this
    drives the outcome they actually return. Asserting only against a fake that
    raises would leave the production path untested, which is how this shipped.
    """
    memory = _FakeMemory(outcome="unavailable")
    out = await _call_remember(memory, "Some durable fact.")
    assert out["saved"] is False
    assert "unavailable" in out["error"]
    # Must not tell the model the write was correctly declined.
    assert "note" not in out


async def test_a_planner_delete_is_not_reported_as_a_save():
    """A delete mutates the store while storing nothing, so 'saved' would name a
    fact that no later recall can find."""
    memory = _FakeMemory(outcome="removed")
    out = await _call_remember(memory, "The user stopped using Slack.")
    assert out["saved"] is False
    assert "removed" in out["note"]
    assert "text" not in out  # never echo back text that was not stored


async def test_a_raising_memory_service_reports_unavailable_rather_than_saved():
    """Defense in depth: the protocol forbids raising, but a third-party
    implementation is only as good as its word."""

    class _Boom(_FakeMemory):
        async def remember(self, *_args, **_kwargs) -> MemoryWriteOutcome:
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
        deployment=DeploymentOption(
            region="eastus2", dataZone="US", sku="GlobalStandard", deploymentName="dep-1"
        ),
        correlation_id="cid",
        email="u@example.com",
        library_document_ids=["doc-1"],
    )

    assert payload["context"]["email"] == "u@example.com"
    assert payload["context"]["libraryDocumentIds"] == ["doc-1"]


# --- Chat-only tools are recorded, not silently dropped -------------------------


def test_every_selectable_synthetic_tool_is_classified() -> None:
    """A new synthetic tool must be declared chat-only or built here.

    This is the guard that matters: the three chat-only names fell through
    `build_shared_capabilities` with no branch and no `unavailable` entry, so a
    workflow step carrying one ran with the tool simply absent.
    """
    shared = {RECALL_TOOL_NAME, REMEMBER_TOOL_NAME}
    assert CHAT_ONLY_SYNTHETIC_TOOL_NAMES | shared == SELECTABLE_SYNTHETIC_TOOL_NAMES
    assert not (CHAT_ONLY_SYNTHETIC_TOOL_NAMES & shared)


def test_chat_only_tools_are_reported_unavailable_to_a_workflow_step() -> None:
    built = build_shared_capabilities(
        attached_tool_names=sorted(CHAT_ONLY_SYNTHETIC_TOOL_NAMES),
        user_id="u1",
        nonce="n",
        session_id="s1",
    )
    # Nothing can be built for them...
    assert built.tools == []
    assert built.handlers == {}
    # ...but the run is no longer left with zero signal.
    assert set(built.unavailable) == set(CHAT_ONLY_SYNTHETIC_TOOL_NAMES)
    assert all("chat only" in reason for reason in built.unavailable.values())


def test_chat_only_reporting_does_not_disturb_tools_that_do_build() -> None:
    """Control: a shared capability alongside a chat-only one still builds."""
    built = build_shared_capabilities(
        attached_tool_names=["remember_memory", "generate_image"],
        user_id="u1",
        nonce="n",
        session_id="s1",
        memory=_FakeMemory(),
    )
    assert [t["function"]["name"] for t in built.tools] == [REMEMBER_TOOL_NAME]
    assert set(built.unavailable) == {"generate_image"}


# --- Unattended runs opt out of per-invocation approval, on purpose -------------


class _BrowsingGateway:
    """Emits one ``browse_url`` call, then a final answer."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self, *, deployment, messages, params=None, correlation_id=None, api="chat"
    ):
        self.calls += 1
        offered = [t["function"]["name"] for t in (params or {}).get("tools", []) or []]
        if self.calls == 1 and "browse_url" in offered:
            return {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "browse_url",
                                        "arguments": '{"url": "https://example.com/"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": _USAGE,
            }
        return {"choices": [{"message": {"content": "done"}}], "usage": _USAGE}

    async def stream(self, **_kwargs):  # pragma: no cover - runner never streams
        raise AssertionError("runner must not stream")


class _BrowsingWebSearch:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def build_capability(self, *, user_id: str, session_id: str, nonce: str):
        async def _handler(args: dict[str, Any], _ctx: ToolContext) -> dict[str, Any]:
            self.calls.append(args)
            return {"content": "page"}

        schema = {
            "type": "function",
            "function": {"name": "browse_url", "parameters": {"type": "object"}},
        }
        return [schema], {"browse_url": _handler}


async def test_a_workflow_step_is_exempt_from_per_invocation_approval() -> None:
    """The one place the P1-13 seam stays open, pinned so it stays a decision.

    ``browse_url`` is the capability that is held on *every* chat turn, tainted or
    not. A workflow step runs it, because ``run_workflow_step`` passes an explicit
    ``ApprovalPolicy.off``: an unattended run has no open request to return a
    grant on and nobody watching to click it, so "hold for approval" there does
    not mean "ask" — it means "deny, silently, forever".

    If someone removes that explicit policy, the default (``always``) takes over
    and this fails, which is the point: reverting the exemption must break a test
    rather than quietly break every workflow that reads or searches.
    """
    web = _BrowsingWebSearch()
    state = type("S", (), {"document_retrieval": None, "web_search": web, "memory": None})()
    builder = capability_builder_for_state(state, user_id="u1", session_id="s1")

    gw = _BrowsingGateway()
    registry, executor = build_tools()
    outcome = await run_workflow_step(
        WorkflowStep(agent="a1", instruction="Do {input}"),
        index=0,
        workflow_name="flow",
        run_input="hello",
        previous="",
        composed=AgentCatalog(agents=[_agent("a1")]),
        deployment="dep-1",
        gateway=gw,
        registry=registry,
        executor=executor,
        capabilities=builder,
        correlation_id="cid",
    )

    assert outcome.result.ok
    assert web.calls == [{"url": "https://example.com/"}]
