"""Unit tests for workflow models/service validation and the workflow runner."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec
from ai4ia_api.agents.tool_exec import build_tools
from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.workflows.models import (
    MAX_INSTRUCTION_LEN,
    MAX_STEP_TOOLS,
    MAX_STEPS,
    MAX_WORKFLOWS_PER_USER,
    WorkflowConflictError,
    WorkflowCreate,
    WorkflowStep,
    WorkflowValidationError,
)
from ai4ia_api.workflows.runner import MAX_CARRY_LEN, run_workflow
from ai4ia_api.workflows.service import WorkflowService
from ai4ia_api.workflows.store import InMemoryWorkflowStore

_USAGE = {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
_UID = "u1"


def _svc(attachable: frozenset[str] | None = None) -> WorkflowService:
    return WorkflowService(
        InMemoryWorkflowStore(),
        attachable_tools=frozenset() if attachable is None else attachable,
    )


def _step(agent: str, instruction: str) -> WorkflowStep:
    return WorkflowStep(agent=agent, instruction=instruction)


def _create(name: str = "flow", steps=None, **over) -> WorkflowCreate:
    if steps is None:
        steps = [_step("a1", "Do {input}")]
    return WorkflowCreate(name=name, steps=steps, **over)


# --- Service / model validation ------------------------------------------------


async def test_create_and_get_roundtrip():
    svc = _svc()
    wf = await svc.create(_UID, _create(displayName="My Flow"))
    assert wf.id == wf.name == "flow"
    assert wf.userId == _UID
    assert wf.displayName == "My Flow"
    fetched = await svc.get(_UID, "FLOW")  # case-insensitive
    assert fetched is not None and fetched.name == "flow"


async def test_create_normalizes_name_and_step_agent():
    svc = _svc()
    wf = await svc.create(
        _UID, _create(name="Flow", steps=[_step("A1", "Use {input}")])
    )
    assert wf.name == "flow"
    assert wf.steps[0].agent == "a1"


@pytest.mark.parametrize("bad", ["", "1abc", "Has Space", "-leading", "x" * 33])
async def test_create_rejects_bad_name(bad):
    with pytest.raises(WorkflowValidationError):
        await _svc().create(_UID, _create(name=bad))


async def test_create_requires_at_least_one_step():
    with pytest.raises(WorkflowValidationError):
        await _svc().create(_UID, _create(steps=[]))


async def test_create_rejects_too_many_steps():
    steps = [_step("a", "{input}")] + [_step("a", "x") for _ in range(MAX_STEPS)]
    with pytest.raises(WorkflowValidationError):
        await _svc().create(_UID, _create(steps=steps))


async def test_first_step_must_reference_input():
    with pytest.raises(WorkflowValidationError, match="first step"):
        await _svc().create(_UID, _create(steps=[_step("a1", "no placeholder")]))


async def test_later_steps_need_not_reference_input():
    svc = _svc()
    wf = await svc.create(
        _UID,
        _create(steps=[_step("a1", "Start {input}"), _step("a2", "Refine {previous}")]),
    )
    assert len(wf.steps) == 2


async def test_create_rejects_overlong_instruction():
    assert MAX_INSTRUCTION_LEN == 4000
    long = "x" * 4001
    with pytest.raises(ValidationError):
        _step("a1", long)


async def test_create_rejects_bad_step_agent_name():
    with pytest.raises(WorkflowValidationError):
        await _svc().create(_UID, _create(steps=[_step("Has Space", "{input}")]))


async def test_create_rejects_blank_instruction():
    with pytest.raises(WorkflowValidationError):
        await _svc().create(_UID, _create(steps=[_step("a1", "   ")]))


# --- Per-step extra tools ------------------------------------------------------


def _tool_step(tools: list[str]) -> WorkflowStep:
    return WorkflowStep(agent="a1", instruction="Do {input}", extraTools=tools)


async def test_a_step_may_add_an_allowlisted_tool():
    svc = _svc(frozenset({"remember_memory", "calculator"}))
    wf = await svc.create(_UID, _create(steps=[_tool_step([" remember_memory "])]))
    assert wf.steps[0].extraTools == ["remember_memory"]


async def test_a_step_may_not_add_a_tool_outside_the_allowlist():
    svc = _svc(frozenset({"calculator"}))
    with pytest.raises(WorkflowValidationError, match="remember_memory"):
        await svc.create(_UID, _create(steps=[_tool_step(["remember_memory"])]))


async def test_mcp_tool_names_are_rejected_as_step_extras():
    """``mcp:`` names are per-user and dynamic, so this service cannot resolve
    them. Rejecting costs nothing: ``extraTools`` is additive, so an agent's own
    MCP tools still reach the step."""
    svc = _svc(frozenset({"remember_memory"}))
    with pytest.raises(WorkflowValidationError):
        await svc.create(_UID, _create(steps=[_tool_step(["mcp:srv/do_thing"])]))


async def test_workflow_tool_cannot_be_nested_inside_a_workflow():
    svc = _svc(frozenset({"run_workflow"}))
    with pytest.raises(WorkflowValidationError, match="cannot be nested"):
        await svc.create(
            _UID, _create(steps=[_tool_step(["run_workflow"])])
        )


async def test_duplicate_step_tools_are_rejected():
    svc = _svc(frozenset({"remember_memory"}))
    with pytest.raises(WorkflowValidationError, match="duplicate"):
        await svc.create(
            _UID, _create(steps=[_tool_step(["remember_memory", "remember_memory"])])
        )


async def test_step_tools_are_capped():
    allowed = frozenset(f"t{i}" for i in range(MAX_STEP_TOOLS + 1))
    svc = _svc(allowed)
    with pytest.raises(WorkflowValidationError, match=str(MAX_STEP_TOOLS)):
        await svc.create(_UID, _create(steps=[_tool_step(sorted(allowed))]))


async def test_a_step_saved_without_extra_tools_stays_unchanged():
    """Back-compat floor: every workflow stored before this field existed
    round-trips with an empty list, so its behaviour is bit-for-bit the same."""
    svc = _svc(frozenset({"remember_memory"}))
    wf = await svc.create(_UID, _create())
    assert wf.steps[0].extraTools == []


async def test_duplicate_name_conflicts():
    svc = _svc()
    await svc.create(_UID, _create())
    with pytest.raises(WorkflowConflictError):
        await svc.create(_UID, _create())


async def test_per_user_cap_fails_closed():
    svc = _svc()
    for i in range(MAX_WORKFLOWS_PER_USER):
        await svc.create(_UID, _create(name=f"flow{i}"))
    with pytest.raises(WorkflowConflictError):
        await svc.create(_UID, _create(name="one-too-many"))


async def test_users_are_isolated():
    svc = _svc()
    await svc.create("alice", _create(name="shared"))
    await svc.create("bob", _create(name="shared"))  # no conflict across users
    assert await svc.get("alice", "shared") is not None
    assert len(await svc.list_for("alice")) == 1
    assert len(await svc.list_for("bob")) == 1


async def test_update_replaces_steps_and_preserves_created_at():
    svc = _svc()
    wf = await svc.create(_UID, _create())
    from ai4ia_api.workflows.models import WorkflowUpdate

    updated = await svc.update(
        _UID, "flow", WorkflowUpdate(steps=[_step("a1", "New {input}")], enabled=False)
    )
    assert updated.createdAt == wf.createdAt
    assert updated.enabled is False
    assert updated.steps[0].instruction == "New {input}"


async def test_update_missing_raises():
    from ai4ia_api.workflows.models import WorkflowNotFoundError, WorkflowUpdate

    with pytest.raises(WorkflowNotFoundError):
        await _svc().update(
            _UID, "ghost", WorkflowUpdate(steps=[_step("a1", "{input}")])
        )


# --- Runner --------------------------------------------------------------------


class _RunnerGateway:
    """Echoes the user message it receives, with a stable usage payload. Records
    every user prompt it saw so tests can assert {input}/{previous} threading."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        user = messages[-1]["content"]
        self.seen.append(user)
        return {"choices": [{"message": {"content": f"echo:{user}"}}], "usage": _USAGE}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):  # pragma: no cover
        raise AssertionError("runner must not stream")


class _RaisingGateway:
    """Succeeds for the first N calls, then raises (to test mid-pipeline failure)."""

    def __init__(self, fail_on_call: int) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        if self.calls >= self.fail_on_call:
            raise RuntimeError("gateway boom")
        return {"choices": [{"message": {"content": "ok"}}], "usage": _USAGE}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):  # pragma: no cover
        raise AssertionError("runner must not stream")


def _catalog(*specs: AgentSpec) -> AgentCatalog:
    return AgentCatalog(agents=list(specs))


def _leaf(name: str) -> AgentSpec:
    return AgentSpec(
        name=name, displayName=name, description="", systemPrompt=f"You are {name}."
    )


async def _run(workflow_steps, *, gateway, composed, run_input="hello"):
    registry, executor = build_tools()
    svc = _svc()
    wf = await svc.create(_UID, _create(steps=workflow_steps))
    return await run_workflow(
        wf,
        run_input=run_input,
        composed=composed,
        deployment="dep-1",
        gateway=gateway,
        registry=registry,
        executor=executor,
        correlation_id="cid",
    )


async def test_runner_threads_input_and_previous_and_sums_usage():
    gw = _RunnerGateway()
    result = await _run(
        [_step("a1", "Process {input}"), _step("a2", "Refine {previous}")],
        gateway=gw,
        composed=_catalog(_leaf("a1"), _leaf("a2")),
    )
    assert result.ok is True
    # step1 sees the rendered {input}; step2 sees the rendered {previous}.
    assert gw.seen[0] == "Process hello"
    assert gw.seen[1] == "Refine echo:Process hello"
    # final output is the last step's output.
    assert result.text == "echo:Refine echo:Process hello"
    # usage summed across both model calls.
    assert result.usage.total == 14
    assert result.usage.calls == 2
    assert result.usage.complete is True


async def test_runner_unknown_step_agent_stops_with_accumulated_usage():
    gw = _RunnerGateway()
    result = await _run(
        [_step("a1", "Start {input}"), _step("ghost", "Refine {previous}")],
        gateway=gw,
        composed=_catalog(_leaf("a1")),  # no 'ghost'
    )
    assert result.ok is False
    assert "ghost" in result.text
    # step1 ran and its usage is retained even though the run failed at step2.
    assert result.usage.total == 7
    assert gw.seen == ["Start hello"]


async def test_runner_disabled_step_agent_stops():
    disabled = AgentSpec(
        name="a2", displayName="a2", description="", systemPrompt="x", enabled=False
    )
    result = await _run(
        [_step("a1", "Start {input}"), _step("a2", "Refine {previous}")],
        gateway=_RunnerGateway(),
        composed=_catalog(_leaf("a1"), disabled),
    )
    assert result.ok is False
    assert "unavailable" in result.text


async def test_runner_rejects_orchestrator_step_agent():
    boss = AgentSpec(
        name="boss",
        displayName="boss",
        description="",
        systemPrompt="x",
        links=["a1"],
    )
    result = await _run(
        [_step("boss", "Coordinate {input}")],
        gateway=_RunnerGateway(),
        composed=_catalog(boss, _leaf("a1")),
    )
    assert result.ok is False
    assert "orchestrator" in result.text


async def test_runner_handles_braces_in_input_without_crashing():
    gw = _RunnerGateway()
    result = await _run(
        [_step("a1", "Echo {input}")],
        gateway=gw,
        composed=_catalog(_leaf("a1")),
        run_input="weird {not_a_key} {0} braces",
    )
    assert result.ok is True
    assert gw.seen[0] == "Echo weird {not_a_key} {0} braces"


async def test_runner_per_step_exception_returns_accumulated_usage():
    gw = _RaisingGateway(fail_on_call=2)  # step1 ok, step2 raises
    result = await _run(
        [_step("a1", "Start {input}"), _step("a2", "Refine {previous}")],
        gateway=gw,
        composed=_catalog(_leaf("a1"), _leaf("a2")),
    )
    assert result.ok is False
    assert result.usage.total == 7  # only step1 counted
    assert "failed" in result.text


async def test_runner_truncates_previous_to_carry_cap():
    big = "B" * (MAX_CARRY_LEN + 500)

    class _BigGateway:
        def __init__(self):
            self.seen = []

        async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
            self.seen.append(messages[-1]["content"])
            # step1 returns an oversized output; step2 should see it truncated.
            return {"choices": [{"message": {"content": big}}], "usage": _USAGE}

        async def stream(self, **_k):  # pragma: no cover
            raise AssertionError

    gw = _BigGateway()
    result = await _run(
        [_step("a1", "Start {input}"), _step("a2", "{previous}")],
        gateway=gw,
        composed=_catalog(_leaf("a1"), _leaf("a2")),
    )
    assert result.ok is True
    # step2's prompt is the truncated previous (exactly MAX_CARRY_LEN chars).
    assert len(gw.seen[1]) == MAX_CARRY_LEN


async def test_runner_blocks_placeholder_amplification():
    # An instruction that repeats {input} many times would expand a bounded input
    # past the rendered-prompt cap; the runner must stop before calling the model.
    repeated = "{input} " * 200  # ~200 * 8000 chars if not capped
    gw = _RunnerGateway()
    result = await _run(
        [_step("a1", repeated)],
        gateway=gw,
        composed=_catalog(_leaf("a1")),
        run_input="x" * 5000,
    )
    assert result.ok is False
    assert "exceeds" in result.text
    assert gw.seen == []  # no model call fired
    assert result.usage.calls == 0


async def test_runner_partial_step_failure_keeps_attempted_call_usage():
    class PartialFailureGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Calculating. ",
                                "tool_calls": [
                                    {
                                        "id": "calc-1",
                                        "type": "function",
                                        "function": {
                                            "name": "calculator",
                                            "arguments": '{"expression": "6*7"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": _USAGE,
                }
            raise RuntimeError("gateway boom")

    gateway = PartialFailureGateway()
    agent = AgentSpec(
        name="a1",
        displayName="a1",
        description="",
        systemPrompt="You calculate.",
        tools=["calculator"],
    )
    result = await _run(
        [_step("a1", "Calculate {input}")],
        gateway=gateway,
        composed=_catalog(agent),
    )

    assert result.ok is False
    assert "failed" in result.text
    assert gateway.calls == 2
    assert (
        result.usage.prompt,
        result.usage.completion,
        result.usage.total,
        result.usage.calls,
        result.usage.complete,
    ) == (3, 4, 7, 2, False)
    assert result.steps[-1].iterations == 2
    assert result.steps[-1].text == "Calculating. "


async def test_runner_first_gateway_failure_counts_unknown_call_without_logging_detail(
    caplog,
):
    marker = "workflow-hostile-gateway-detail"

    class FirstFailureGateway:
        async def complete(self, **_kwargs):
            raise ModelGatewayError(502, marker)

    result = await _run(
        [_step("a1", "Process {input}")],
        gateway=FirstFailureGateway(),
        composed=_catalog(_leaf("a1")),
    )

    assert result.ok is False
    assert (result.usage.calls, result.usage.known, result.usage.complete) == (
        1,
        False,
        False,
    )
    assert "status=502" in caplog.text
    assert marker not in caplog.text
