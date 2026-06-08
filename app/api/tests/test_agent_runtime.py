"""Tests for the agent tool-calling runtime.

A ``ScriptedGateway`` returns a pre-programmed sequence of chat-completion
responses so we can drive the tool loop deterministically and assert that tool
results are fed back, denials/errors are surfaced as tool messages, and bounds
are honored.
"""
from __future__ import annotations

import json

from ai4ia_api.agents.runtime import run_agent_turn
from ai4ia_api.agents.tool_exec import ToolContext, ToolDefinition, ToolExecutor, build_tools
from ai4ia_api.agents.tools import ToolRegistry, ToolSpec


def _assistant_tool_call(call_id: str, name: str, arguments: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            }
        ]
    }


def _assistant_text(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _usage(p: int, c: int) -> dict:
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


class ScriptedGateway:
    """Returns queued responses in order; records each call's messages + params."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, *, deployment, messages, params=None, correlation_id=None):
        self.calls.append({"messages": [dict(m) for m in messages], "params": params or {}})
        if not self._responses:
            return _assistant_text("(no more scripted responses)")
        return self._responses.pop(0)


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "You are the analyst."},
        {"role": "user", "content": "What is 6*7?"},
    ]


async def test_runs_tool_then_returns_final_answer():
    registry, executor = build_tools()
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "calculator", json.dumps({"expression": "6*7"})),
            _assistant_text("The answer is 42."),
        ]
    )
    result = await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
    )
    assert result.text == "The answer is 42."
    assert result.iterations == 2
    # A tool_result step was recorded with the computed value.
    kinds = [s.kind for s in result.steps]
    assert "tool_result" in kinds and kinds[-1] == "final"
    tool_step = next(s for s in result.steps if s.kind == "tool_result")
    assert tool_step.result == {"expression": "6*7", "result": 42}

    # The second model call saw the assistant tool_call message AND a matching
    # role:"tool" result keyed by the same id.
    second_msgs = gateway.calls[1]["messages"]
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in second_msgs)
    tool_msgs = [m for m in second_msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0]["tool_call_id"] == "c1"
    assert json.loads(tool_msgs[0]["content"])["result"] == 42


async def test_first_call_advertises_only_allowlisted_tools():
    registry, executor = build_tools()
    gateway = ScriptedGateway([_assistant_text("hi")])
    await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
    )
    tools = gateway.calls[0]["params"].get("tools")
    names = {t["function"]["name"] for t in tools}
    assert names == {"calculator"}
    assert gateway.calls[0]["params"]["tool_choice"] == "auto"


async def test_caller_tools_params_are_stripped():
    registry, executor = build_tools()
    gateway = ScriptedGateway([_assistant_text("hi")])
    await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=[],  # no tools -> runtime must not advertise any
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        params={"tools": [{"type": "function", "function": {"name": "evil"}}], "temperature": 0.2},
    )
    params = gateway.calls[0]["params"]
    assert "tools" not in params  # caller-injected tools dropped, schema empty
    assert params["temperature"] == 0.2


async def test_denied_tool_is_not_executed_and_surfaced():
    # Register a tool in the executor whose spec requires a scope the ctx lacks,
    # but force the model to call it anyway (schema filtering is separate from
    # execution-time authorization, which must still deny).
    registry = ToolRegistry()
    executor = ToolExecutor()
    ran = {"count": 0}

    def handler(args, ctx):
        ran["count"] += 1
        return {"ok": True}

    d = ToolDefinition(
        spec=ToolSpec(name="locked", description="d", scopes=frozenset({"admin"})),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )
    registry.register(d.spec)
    executor.register(d)

    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "locked", "{}"),
            _assistant_text("Sorry, I can't do that."),
        ]
    )
    result = await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["locked"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),  # no scopes granted
    )
    assert ran["count"] == 0  # never executed
    assert any(s.kind == "tool_denied" for s in result.steps)
    tool_msg = [m for m in gateway.calls[1]["messages"] if m.get("role") == "tool"][0]
    payload = json.loads(tool_msg["content"])
    assert payload["error"]["type"] == "authorization_denied"


async def test_malformed_arguments_yield_one_tool_result_per_call():
    registry, executor = build_tools()
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "calculator", "{not valid json"),
            _assistant_text("Could not compute."),
        ]
    )
    result = await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
    )
    assert result.text == "Could not compute."
    tool_msgs = [m for m in gateway.calls[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert json.loads(tool_msgs[0]["content"])["error"]["type"] == "invalid_arguments"


async def test_execution_error_surfaced_as_tool_result():
    registry, executor = build_tools()
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "calculator", json.dumps({"expression": "1/0"})),
            _assistant_text("That divides by zero."),
        ]
    )
    result = await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
    )
    assert result.text == "That divides by zero."
    tool_msg = [m for m in gateway.calls[1]["messages"] if m.get("role") == "tool"][0]
    assert json.loads(tool_msg["content"])["error"]["type"] == "execution_error"


async def test_multiple_tool_calls_in_one_message_each_answered():
    registry, executor = build_tools()
    multi = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "a",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": json.dumps({"expression": "1+1"}),
                            },
                        },
                        {
                            "id": "b",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": json.dumps({"expression": "2+2"}),
                            },
                        },
                    ],
                }
            }
        ]
    }
    gateway = ScriptedGateway([multi, _assistant_text("2 and 4.")])
    result = await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
    )
    assert result.text == "2 and 4."
    tool_msgs = [m for m in gateway.calls[1]["messages"] if m.get("role") == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"a", "b"}


async def test_max_iters_forces_final_call_without_tools():
    registry, executor = build_tools()
    # The model keeps asking for the tool every turn; runtime must bound it and
    # make a final tools-disabled call to extract an answer.
    loop = _assistant_tool_call("c", "calculator", json.dumps({"expression": "1+1"}))
    gateway = ScriptedGateway([loop, loop, _assistant_text("forced final")])
    result = await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        max_iters=2,
    )
    assert result.iterations == 2
    assert result.steps[-1].detail == "max_iters"
    # The final (3rd) call must NOT advertise tools.
    assert "tools" not in gateway.calls[-1]["params"]
    assert result.text == "forced final"


async def test_usage_summed_across_calls_complete_when_all_report():
    registry, executor = build_tools()
    tool_call = _assistant_tool_call("c1", "calculator", json.dumps({"expression": "6*7"}))
    tool_call["usage"] = _usage(100, 20)
    final = _assistant_text("The answer is 42.")
    final["usage"] = _usage(50, 10)
    gateway = ScriptedGateway([tool_call, final])
    result = await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
    )
    assert result.usage.known is True
    assert result.usage.complete is True
    assert result.usage.calls == 2
    assert result.usage.prompt == 150
    assert result.usage.completion == 30
    assert result.usage.total == 180


async def test_usage_incomplete_when_one_call_omits_usage():
    registry, executor = build_tools()
    tool_call = _assistant_tool_call("c1", "calculator", json.dumps({"expression": "6*7"}))
    tool_call["usage"] = _usage(100, 20)
    final = _assistant_text("The answer is 42.")  # no usage on the final call
    gateway = ScriptedGateway([tool_call, final])
    result = await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
    )
    assert result.usage.known is True  # the first call reported
    assert result.usage.complete is False  # but the final call did not
    assert result.usage.calls == 2
    assert result.usage.total == 120  # only the known call's tokens


async def test_usage_summed_on_max_iters_final_call():
    registry, executor = build_tools()
    loop = _assistant_tool_call("c", "calculator", json.dumps({"expression": "1+1"}))
    loop["usage"] = _usage(10, 5)
    forced = _assistant_text("forced final")
    forced["usage"] = _usage(7, 3)
    gateway = ScriptedGateway([loop, loop, forced])
    result = await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        max_iters=2,
    )
    # 2 loop calls + 1 forced final call all contribute.
    assert result.usage.calls == 3
    assert result.usage.complete is True
    assert result.usage.total == (15 + 15 + 10)
