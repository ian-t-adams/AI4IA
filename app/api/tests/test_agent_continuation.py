import json

import pytest

from ai4ia_api.agents.approvals import approval_key, arguments_digest
from ai4ia_api.agents.runtime import AgentRunFailed, AgentRunPaused, run_agent_turn
from ai4ia_api.agents.tool_exec import ToolContext, ToolDefinition, build_tools
from ai4ia_api.agents.tools import ToolRisk, ToolSpec
from ai4ia_api.agents.turn_checkpoint import TurnCheckpoint
from tests.test_agent_runtime import ScriptedGateway, _assistant_text, _assistant_tool_call, _messages


class StoredController:
    """Round-trip state, rather than holding a Python stack or a live grant."""

    visible_resource_ids = frozenset()

    def __init__(self, serialized=None):
        self.serialized = serialized
        self.restored = TurnCheckpoint.from_persisted(json.loads(serialized)) if serialized else None
        self.operations = []

    def save(self, state):
        self.serialized = state.model_dump_json()

    async def before_model(self, state, params):
        self.operations.append(("model", state.iterations))
        self.save(state)

    async def model_completed(self, state):
        self.save(state)

    async def before_tool(self, state, **kwargs):
        self.operations.append(("tool", state.iterations, state.nextToolIndex))
        self.save(state)

    async def tool_completed(self, state, **kwargs):
        self.save(state)

    async def hold(self, state, **kwargs):
        self.save(state)


def setup_calls():
    sent = []

    async def handler(args, ctx):
        sent.append(args["text"])
        return {"sent": args["text"]}

    registry, executor = build_tools([ToolDefinition(
        ToolSpec(name="send", description="Send", risk=ToolRisk.external, requires_approval=True),
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        handler,
    )])
    batch = _assistant_tool_call("a", "calculator", '{"expression":"2+3"}')
    calls = batch["choices"][0]["message"]["tool_calls"]
    calls.extend(_assistant_tool_call("b", "send", '{"text":"hello"}')["choices"][0]["message"]["tool_calls"])
    calls.extend(_assistant_tool_call("c", "calculator", '{"expression":"3+4"}')["choices"][0]["message"]["tool_calls"])
    batch["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    final = _assistant_text("done")
    final["usage"] = {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22}
    return registry, executor, sent, batch, final


async def test_pause_in_batch_restores_same_call_without_reissuing_model_or_prior_tool():
    registry, executor, sent, batch, final = setup_calls()
    first_gateway = ScriptedGateway([batch])
    first = StoredController()
    kwargs = dict(
        deployment="m", messages=_messages(), tool_names=["calculator", "send"],
        registry=registry, executor=executor, max_iters=2,
    )
    with pytest.raises(AgentRunPaused) as pending:
        await run_agent_turn(
            **kwargs, gateway=first_gateway, ctx=ToolContext(turn_budgets={}), checkpoint=first,
        )
    assert sent == []
    assert len(first_gateway.calls) == 1
    assert pending.value.partial.usage.total == 15
    assert [step.result["result"] for step in pending.value.partial.steps] == [5]
    assert first.restored is None
    restored = StoredController(first.serialized)
    assert restored.restored.nextToolIndex == 1
    assert restored.restored.currentToolCounted is True
    key = approval_key("send", arguments_digest({"text": "hello"}))
    resumed_gateway = ScriptedGateway([final])
    resumed = await run_agent_turn(
        **kwargs, gateway=resumed_gateway,
        ctx=ToolContext(invocation_approvals=frozenset({key}), turn_budgets={}),
        checkpoint=restored,
    )
    assert sent == ["hello"]
    assert resumed.usage.total == 37
    assert resumed.usage.calls == 2
    assert len(resumed_gateway.calls) == 1
    assert len([step for step in resumed.steps if step.tool == "calculator"]) == 2
    assert [step.approval for step in resumed.steps if step.tool == "send"] == ["invocation"]
    model_tools = [m for m in resumed_gateway.calls[0]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in model_tools] == ["a", "b", "c"]


async def test_restarting_without_a_fresh_exact_grant_remains_paused():
    registry, executor, sent, batch, _ = setup_calls()
    state = StoredController()
    kwargs = dict(
        deployment="m", messages=_messages(), tool_names=["calculator", "send"],
        registry=registry, executor=executor, max_iters=2, ctx=ToolContext(turn_budgets={}),
    )
    with pytest.raises(AgentRunPaused):
        await run_agent_turn(**kwargs, gateway=ScriptedGateway([batch]), checkpoint=state)
    resumed_gateway = ScriptedGateway([])
    with pytest.raises(AgentRunPaused):
        await run_agent_turn(
            **kwargs, gateway=resumed_gateway, checkpoint=StoredController(state.serialized),
        )
    assert resumed_gateway.calls == []
    assert sent == []


async def test_ambiguous_model_checkpoint_is_never_dispatched_again():
    registry, executor, _, batch, _ = setup_calls()
    state = StoredController()

    class LostModel(ScriptedGateway):
        async def complete(self, **kwargs):
            await super().complete(**kwargs)
            raise ConnectionError("response lost")

    kwargs = dict(
        deployment="m", messages=_messages(), tool_names=["calculator", "send"],
        registry=registry, executor=executor, max_iters=2,
        ctx=ToolContext(turn_budgets={}), retain_failed_request=True,
    )
    with pytest.raises(AgentRunFailed):
        await run_agent_turn(**kwargs, gateway=LostModel([batch]), checkpoint=state)
    restored = StoredController(state.serialized)
    assert restored.restored.phase == "model"
    gateway = ScriptedGateway([batch])
    with pytest.raises(AgentRunFailed, match="model round trip failed"):
        await run_agent_turn(**kwargs, gateway=gateway, checkpoint=restored)
    assert gateway.calls == []


def test_incomplete_persisted_counters_are_not_defaulted():
    with pytest.raises(ValueError, match="Incomplete persisted"):
        TurnCheckpoint.from_persisted({"version": 1, "phase": "ready"})
