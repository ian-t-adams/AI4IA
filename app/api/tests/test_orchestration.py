"""Unit tests for the multi-agent delegation capability (Phase 8 inc 2).

These exercise :mod:`ai4ia_api.agents.orchestration` directly (no HTTP), driving
the ``delegate_to_agent`` handler against a scripted gateway and the real tool
registry/executor so the runtime sub-turn path is covered end to end.
"""
from __future__ import annotations

import pytest

from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec
from ai4ia_api.agents.orchestration import (
    DELEGATE_TOOL_NAME,
    MAX_DELEGATIONS_PER_TURN,
    build_delegate_capability,
    sanitize_links,
)
from ai4ia_api.agents.tool_exec import ToolContext, build_tools


class _Gateway:
    """Returns a fixed final answer for every sub-turn and counts calls."""

    def __init__(self, text: str = "sub answer") -> None:
        self.text = text
        self.calls = 0
        self.last_messages = None

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        self.last_messages = messages
        return {
            "choices": [{"message": {"role": "assistant", "content": self.text}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }


def _catalog(*specs: AgentSpec) -> AgentCatalog:
    return AgentCatalog(agents=list(specs))


def _orchestrator(links: list[str]) -> AgentSpec:
    return AgentSpec(
        name="boss",
        displayName="Boss",
        description="supervisor",
        systemPrompt="You coordinate.",
        links=links,
        tools=[],
    )


def _leaf(name: str, *, enabled: bool = True) -> AgentSpec:
    return AgentSpec(
        name=name,
        displayName=name,
        description="leaf",
        systemPrompt=f"You are {name}.",
        tools=[],
        enabled=enabled,
    )


def _build(orch: AgentSpec, composed: AgentCatalog, gw: _Gateway):
    registry, executor = build_tools()
    return build_delegate_capability(
        orchestrator=orch,
        composed=composed,
        gateway=gw,
        registry=registry,
        executor=executor,
        deployment="boss-deployment",
    )


# --- sanitize_links ----------------------------------------------------------


def test_sanitize_links_normalizes_and_drops_self_and_dupes():
    out = sanitize_links("Boss", ["Helper", "helper", "boss", "BOSS", "an_alyst"])
    assert out == ["helper", "an_alyst"]


def test_sanitize_links_drops_invalid_names():
    assert sanitize_links("boss", ["Bad Name", "", "ok"]) == ["ok"]


def test_sanitize_links_caps_at_max():
    from ai4ia_api.agents.user_agents import MAX_LINKS

    many = [f"a{i}" for i in range(MAX_LINKS + 3)]
    assert sanitize_links("boss", many) == many[:MAX_LINKS]


# --- capability construction -------------------------------------------------


def test_no_links_builds_no_capability():
    tools, handlers, sink = _build(_orchestrator([]), _catalog(), _Gateway())
    assert tools == []
    assert handlers == {}
    assert sink == []


def test_links_build_single_delegate_tool_with_enum():
    tools, handlers, _ = _build(
        _orchestrator(["helper"]), _catalog(_leaf("helper")), _Gateway()
    )
    assert len(tools) == 1
    fn = tools[0]["function"]
    assert fn["name"] == DELEGATE_TOOL_NAME
    assert fn["parameters"]["properties"]["agent"]["enum"] == ["helper"]
    assert set(handlers) == {DELEGATE_TOOL_NAME}


# --- handler behaviour -------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_happy_path_runs_subagent_and_records_usage():
    gw = _Gateway(text="42")
    _, handlers, sink = _build(
        _orchestrator(["helper"]), _catalog(_leaf("helper")), gw
    )
    ctx = ToolContext(correlation_id="cid")
    result = await handlers[DELEGATE_TOOL_NAME]({"agent": "helper", "task": "what is 6*7?"}, ctx)

    assert result == {"agent": "helper", "answer": "42"}
    assert gw.calls == 1
    # The sub-agent saw ONLY its own system prompt + the task (no parent history).
    assert gw.last_messages == [
        {"role": "system", "content": "You are helper."},
        {"role": "user", "content": "what is 6*7?"},
    ]
    assert len(sink) == 1
    assert sink[0].total == 7


@pytest.mark.asyncio
async def test_handler_unknown_link_returns_error_without_calling_model():
    gw = _Gateway()
    _, handlers, sink = _build(
        _orchestrator(["helper"]), _catalog(_leaf("helper")), gw
    )
    # 'other' is a valid name but not in the orchestrator's links.
    result = await handlers[DELEGATE_TOOL_NAME](
        {"agent": "other", "task": "x"}, ToolContext()
    )
    assert "error" in result
    assert gw.calls == 0
    assert sink == []


@pytest.mark.asyncio
async def test_handler_linked_but_missing_target_is_unavailable():
    gw = _Gateway()
    # 'helper' is linked but absent from the composed catalog (e.g. deleted).
    _, handlers, sink = _build(_orchestrator(["helper"]), _catalog(), gw)
    result = await handlers[DELEGATE_TOOL_NAME](
        {"agent": "helper", "task": "x"}, ToolContext()
    )
    assert "error" in result and "unavailable" in result["error"]
    assert gw.calls == 0


@pytest.mark.asyncio
async def test_handler_disabled_target_is_unavailable():
    gw = _Gateway()
    _, handlers, _ = _build(
        _orchestrator(["helper"]), _catalog(_leaf("helper", enabled=False)), gw
    )
    result = await handlers[DELEGATE_TOOL_NAME](
        {"agent": "helper", "task": "x"}, ToolContext()
    )
    assert "error" in result
    assert gw.calls == 0


@pytest.mark.asyncio
async def test_handler_empty_task_rejected():
    gw = _Gateway()
    _, handlers, _ = _build(
        _orchestrator(["helper"]), _catalog(_leaf("helper")), gw
    )
    result = await handlers[DELEGATE_TOOL_NAME](
        {"agent": "helper", "task": "   "}, ToolContext()
    )
    assert "error" in result
    assert gw.calls == 0


@pytest.mark.asyncio
async def test_handler_enforces_delegation_budget():
    gw = _Gateway()
    _, handlers, sink = _build(
        _orchestrator(["helper"]), _catalog(_leaf("helper")), gw
    )
    h = handlers[DELEGATE_TOOL_NAME]
    for _ in range(MAX_DELEGATIONS_PER_TURN):
        ok = await h({"agent": "helper", "task": "go"}, ToolContext())
        assert ok.get("answer") is not None
    # One past the budget: refused, model not called again.
    over = await h({"agent": "helper", "task": "go"}, ToolContext())
    assert "error" in over and "budget" in over["error"]
    assert gw.calls == MAX_DELEGATIONS_PER_TURN
    assert len(sink) == MAX_DELEGATIONS_PER_TURN
