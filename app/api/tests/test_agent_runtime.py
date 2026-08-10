"""Tests for the agent tool-calling runtime.

A ``ScriptedGateway`` returns a pre-programmed sequence of chat-completion
responses so we can drive the tool loop deterministically and assert that tool
results are fed back, denials/errors are surfaced as tool messages, and bounds
are honored.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from ai4ia_api.agents.prompt_budget import (
    message_budget_bytes,
    serialized_budget_bytes,
)
from ai4ia_api.agents.runtime import (
    AgentContextBudgetError,
    AgentRunFailed,
    run_agent_turn,
)
from ai4ia_api.agents.tool_exec import ToolContext, ToolDefinition, ToolExecutor, build_tools
from ai4ia_api.agents.tools import ToolRegistry, ToolSpec
from ai4ia_api.gateway.client import ChatChunk, ModelGatewayError
from tests.conftest import stream_like_gateway


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

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
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


async def test_oversized_actual_tool_schema_fails_before_provider_call():
    async def handler(args, ctx):
        return {"ok": True}

    definition = ToolDefinition(
        spec=ToolSpec(name="wide_schema", description="wide"),
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string", "description": "x" * 2000}},
        },
        handler=handler,
    )
    registry, executor = build_tools([definition])
    gateway = ScriptedGateway([_assistant_text("must not be called")])

    with pytest.raises(AgentContextBudgetError, match="offered tool schemas"):
        await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=["wide_schema"],
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ToolContext(),
            prompt_budget_bytes=512,
        )

    assert gateway.calls == []


async def test_tool_result_growth_rebounds_every_iteration_without_overflow():
    async def handler(args, ctx):
        return {"payload": "r" * 7000}

    definition = ToolDefinition(
        spec=ToolSpec(name="large_result", description="large"),
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    registry, executor = build_tools([definition])
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "large_result", "{}"),
            _assistant_tool_call("c2", "large_result", "{}"),
            _assistant_text("finished normally"),
        ]
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old-u" * 600},
        {"role": "assistant", "content": "old-a" * 600},
        {"role": "user", "content": "current"},
    ]
    budget = 10_000

    result = await run_agent_turn(
        deployment="dep",
        messages=messages,
        tool_names=["large_result"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        prompt_budget_bytes=budget,
    )

    assert result.text == "finished normally"
    assert result.iterations == 3
    assert "old-u" in gateway.calls[0]["messages"][1]["content"]
    second = gateway.calls[1]
    assert all("old-u" not in str(message.get("content")) for message in second["messages"])
    assert any(message.get("tool_calls") for message in second["messages"])
    assert any(message.get("role") == "tool" for message in second["messages"])
    schema_bytes = serialized_budget_bytes(
        {"tools": second["params"]["tools"], "tool_choice": "auto"}
    )
    assert (
        sum(message_budget_bytes(message) for message in second["messages"])
        + schema_bytes
        <= budget
    )
    third = gateway.calls[2]
    assistant_ids = {
        call["id"]
        for message in third["messages"]
        for call in message.get("tool_calls") or []
    }
    tool_ids = {
        message["tool_call_id"]
        for message in third["messages"]
        if message.get("role") == "tool"
    }
    assert assistant_ids == tool_ids == {"c2"}
    assert (
        sum(message_budget_bytes(message) for message in third["messages"])
        + schema_bytes
        <= budget
    )


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


async def test_denied_tool_is_not_executed_and_surfaced(monkeypatch):
    events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "ai4ia_api.agents.runtime.emit_security_block",
        lambda category, reason, source: events.append((category, reason, source)),
    )
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
    assert events == [
        ("tool_authorization", "missing_scopes", "agent_runtime")
    ]


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


async def test_on_step_emits_tool_start_before_result_and_final_live_only():
    registry, executor = build_tools()
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "calculator", json.dumps({"expression": "6*7"})),
            _assistant_text("The answer is 42."),
        ]
    )
    emitted: list[tuple[str, str | None]] = []

    async def on_step(step):
        emitted.append((step.kind, step.tool))

    result = await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        on_step=on_step,
    )
    assert result.text == "The answer is 42."
    # Live: a pre-execution start precedes the finalized result; final closes it.
    assert [k for k, _ in emitted] == ["tool_start", "tool_result", "final"]
    assert ("tool_start", "calculator") in emitted
    # tool_start is live-only; the persisted trace never includes it.
    assert [s.kind for s in result.steps] == ["tool_result", "final"]


async def test_on_step_failure_never_breaks_the_turn():
    registry, executor = build_tools()
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "calculator", json.dumps({"expression": "6*7"})),
            _assistant_text("42."),
        ]
    )

    async def boom(_step):
        raise RuntimeError("ui blew up")

    result = await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        on_step=boom,
    )
    assert result.text == "42."


async def test_tool_ran_log_never_includes_argument_content(caplog):
    """Regression: 'agent tool ran' logged raw (redact_obj'd) arguments at INFO,
    persisting ordinary tool-argument content (e.g. a search query or prompt)
    into application logs. The log line must carry only the tool name."""
    registry, executor = build_tools()
    marker = "super-secret-expression-marker-should-not-be-logged"
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "calculator", json.dumps({"expression": "1+1", "note": marker})),
            _assistant_text("2."),
        ]
    )
    with caplog.at_level(logging.INFO, logger="ai4ia_api.agents.runtime"):
        await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=["calculator"],
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ToolContext(),
        )
    ran_records = [r for r in caplog.records if "agent tool ran" in r.getMessage()]
    assert len(ran_records) == 1
    assert ran_records[0].getMessage() == "agent tool ran: tool=calculator"
    assert marker not in ran_records[0].getMessage()
    # No record anywhere in this turn carries the marker value.
    assert all(marker not in r.getMessage() for r in caplog.records)


def _hostile_marker(tag: str) -> str:
    """An adversarial argument value: a credential-shaped substring plus an
    embedded fake log line (a naive log call could be tricked into emitting
    something that looks like a second, forged record), plus enough repeated
    content to also exercise a long/unbounded value."""
    return (
        f"sk-hostile-secret-{tag}\n"
        f"INFO ai4ia_api.agents.runtime agent tool ran: tool=admin_backdoor\n"
        + ("x" * 500)
    )


async def test_tool_denied_log_never_includes_argument_content(caplog):
    """Coverage hardening: unlike 'agent tool ran'/'agent delegated' (which
    embedded ``redact_obj(parsed)`` pre-fix and needed the round-4 privacy fix
    to stop leaking arguments), 'agent tool denied' has only ever logged
    ``tool=%s reason=%s`` and never took arguments as a format argument. This
    test had no dedicated coverage though, so add it to lock in the guarantee
    and guard against a future change accidentally adding an ``args=%s``
    interpolation here too. Uses adversarial input (credential-shaped values,
    embedded fake log lines, long payloads) for defense in depth."""
    registry = ToolRegistry()
    executor = ToolExecutor()

    def handler(args, ctx):
        return {"ok": True}

    d = ToolDefinition(
        spec=ToolSpec(name="locked", description="d", scopes=frozenset({"admin"})),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )
    registry.register(d.spec)
    executor.register(d)

    hostile = _hostile_marker("denied")
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "locked", json.dumps({"password": hostile})),
            _assistant_text("Sorry, I can't do that."),
        ]
    )
    with caplog.at_level(logging.INFO, logger="ai4ia_api.agents.runtime"):
        await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=["locked"],
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ToolContext(),  # no scopes granted
        )
    denied_records = [r for r in caplog.records if "agent tool denied" in r.getMessage()]
    assert len(denied_records) == 1
    assert denied_records[0].getMessage() == "agent tool denied: tool=locked reason=missing_scopes"
    # No record anywhere carries the hostile payload or its forged log line.
    assert all(
        "sk-hostile-secret" not in r.getMessage() and "admin_backdoor" not in r.getMessage()
        for r in caplog.records
    )


async def test_tool_error_log_never_includes_argument_content(caplog):
    """Coverage hardening: 'agent tool error' (a real tool's execution
    -exception path) has only ever logged ``tool=%s`` -- it never took
    arguments or the exception message as a format argument, unlike the two
    sites the round-4 privacy fix had to change. No dedicated test existed
    though; add one to lock in the guarantee against a future regression,
    using adversarial arguments and exception text for defense in depth."""
    registry = ToolRegistry()
    executor = ToolExecutor()
    hostile = _hostile_marker("error")

    def handler(args, ctx):
        raise RuntimeError("boom: " + args.get("payload", ""))

    d = ToolDefinition(
        spec=ToolSpec(name="explode", description="d", scopes=frozenset()),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )
    registry.register(d.spec)
    executor.register(d)

    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "explode", json.dumps({"payload": hostile})),
            _assistant_text("That failed."),
        ]
    )
    with caplog.at_level(logging.INFO, logger="ai4ia_api.agents.runtime"):
        await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=["explode"],
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ToolContext(),
        )
    error_records = [r for r in caplog.records if "agent tool error" in r.getMessage()]
    assert len(error_records) == 1
    assert error_records[0].getMessage() == "agent tool error: tool=explode"
    assert all(
        "sk-hostile-secret" not in r.getMessage() and "admin_backdoor" not in r.getMessage()
        for r in caplog.records
    )


async def test_delegate_error_log_never_includes_argument_content(caplog):
    """Coverage hardening: 'agent delegate error' (the synthetic-handler
    exception path) has only ever logged ``tool=%s`` -- it never took
    arguments or the exception message as a format argument, unlike 'agent
    delegated' (the success path), which did leak ``redact_obj(parsed)``
    pre-fix. No dedicated test existed for this error path though; add one to
    lock in the guarantee, using adversarial arguments for defense in depth."""
    registry = ToolRegistry()
    executor = ToolExecutor()
    hostile = _hostile_marker("delegate")

    async def handler(args, ctx):
        raise RuntimeError("delegate boom: " + args.get("prompt", ""))

    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "delegate_to_agent", json.dumps({"prompt": hostile})),
            _assistant_text("done."),
        ]
    )
    with caplog.at_level(logging.INFO, logger="ai4ia_api.agents.runtime"):
        await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=[],
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ToolContext(),
            extra_tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "delegate_to_agent",
                        "description": "d",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            extra_handlers={"delegate_to_agent": handler},
        )
    error_records = [r for r in caplog.records if "agent delegate error" in r.getMessage()]
    assert len(error_records) == 1
    assert error_records[0].getMessage() == "agent delegate error: tool=delegate_to_agent"
    assert all(
        "sk-hostile-secret" not in r.getMessage() and "admin_backdoor" not in r.getMessage()
        for r in caplog.records
    )


async def test_delegate_log_never_includes_argument_content(caplog):
    """Regression: 'agent delegated' logged raw (redact_obj'd) arguments at
    INFO. Same fix as the tool_result path, covering the synthetic-handler
    (delegate) branch."""
    registry = ToolRegistry()
    executor = ToolExecutor()
    marker = "secret-delegate-prompt-should-not-be-logged"

    async def handler(args, ctx):
        return {"ok": True}

    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "delegate_to_agent", json.dumps({"prompt": marker})),
            _assistant_text("done."),
        ]
    )
    with caplog.at_level(logging.INFO, logger="ai4ia_api.agents.runtime"):
        await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=[],
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ToolContext(),
            extra_tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "delegate_to_agent",
                        "description": "d",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            extra_handlers={"delegate_to_agent": handler},
        )
    delegated_records = [r for r in caplog.records if "agent delegated" in r.getMessage()]
    assert len(delegated_records) == 1
    assert delegated_records[0].getMessage() == "agent delegated: tool=delegate_to_agent"
    assert all(marker not in r.getMessage() for r in caplog.records)


async def test_unknown_tool_name_replaced_with_sentinel_in_denied_step_and_log(caplog):
    """Regression: the raw ``name`` read from the model's tool_call (before any
    registry/handler check) was used directly in ``AgentStep``, ``logger``, and
    ``emit_custom_event`` calls. A hallucinated or adversarially-crafted tool
    name -- never registered as a synthetic handler or in the tool registry --
    must never reach any of those surfaces verbatim; only the fixed
    ``"unknown_tool"`` sentinel may. This name is unknown, so it is denied with
    ``DenyReason.unknown_tool`` (never dispatched)."""
    registry, executor = build_tools()
    hostile_name = (
        "sk-hostile-secret-toolname\n"
        "INFO ai4ia_api.agents.runtime agent tool ran: tool=admin_backdoor"
    )
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", hostile_name, "{}"),
            _assistant_text("Sorry, I can't do that."),
        ]
    )
    with caplog.at_level(logging.INFO, logger="ai4ia_api.agents.runtime"):
        result = await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=["calculator"],
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ToolContext(),
        )
    denied_step = next(s for s in result.steps if s.kind == "tool_denied")
    assert denied_step.tool == "unknown_tool"
    denied_records = [r for r in caplog.records if "agent tool denied" in r.getMessage()]
    assert len(denied_records) == 1
    assert denied_records[0].getMessage() == "agent tool denied: tool=unknown_tool reason=unknown_tool"
    # The raw hallucinated name (and its embedded forged log line) never
    # reaches any log record or persisted step.
    assert all(hostile_name not in r.getMessage() for r in caplog.records)
    assert all("admin_backdoor" not in r.getMessage() for r in caplog.records)
    assert all(s.tool != hostile_name for s in result.steps)


async def test_unknown_tool_name_replaced_with_sentinel_in_live_tool_start():
    """Same guarantee as above, but for the pre-execution ``tool_start``
    marker, which fires (live-only, via ``on_step``) before either the
    synthetic-handler or registry check runs -- the earliest point an
    unvalidated name could leak."""
    registry, executor = build_tools()
    hostile_name = "totally made up tool the model hallucinated"
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", hostile_name, "{}"),
            _assistant_text("Sorry, I can't do that."),
        ]
    )
    emitted: list[tuple[str, str | None]] = []

    async def on_step(step):
        emitted.append((step.kind, step.tool))

    await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        on_step=on_step,
    )
    starts = [tool for kind, tool in emitted if kind == "tool_start"]
    assert starts == ["unknown_tool"]
    assert hostile_name not in starts


async def test_known_tool_name_is_unaffected_by_sentinel_mapping():
    """A registered tool's name must pass through unchanged (``safe_name ==
    name``) -- the sentinel only ever replaces names that are neither a
    synthetic handler nor a registered tool."""
    registry, executor = build_tools()
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", "calculator", json.dumps({"expression": "6*7"})),
            _assistant_text("42."),
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
    tool_step = next(s for s in result.steps if s.kind == "tool_result")
    assert tool_step.tool == "calculator"


def _register_dynamic_tool(name: str) -> tuple[ToolRegistry, ToolExecutor]:
    """A minimal (registry, executor) pair simulating a single dynamically
    discovered/registered tool (e.g. one advertised by an MCP server), whose
    name -- unlike the code-reviewed built-ins -- is not authored by AI4IA."""
    registry = ToolRegistry()
    executor = ToolExecutor()
    spec = ToolSpec(name=name, description="a dynamically registered tool")
    registry.register(spec)
    executor.register(
        ToolDefinition(
            spec=spec,
            parameters={"type": "object", "properties": {}},
            handler=lambda args, ctx: {"ok": True},
        )
    )
    return registry, executor


async def test_registered_tool_with_malicious_name_is_still_sentineled(caplog):
    """Regression: registry/handler *membership* was treated as sufficient proof
    a tool name is safe to log/persist. A name that originates from a source
    this codebase does not author or code-review (e.g. one an MCP server
    advertises at discovery time) can be genuinely registered -- and therefore
    dispatchable -- while still containing newlines or other content crafted to
    forge a different log line. Such a name must be sentineled to
    ``"unknown_tool"`` in every logged/persisted surface even though the call
    itself is authorized and executes successfully (this is NOT a denial)."""
    hostile_name = (
        "weather\nINFO ai4ia_api.agents.runtime agent tool ran: tool=admin_backdoor"
    )
    registry, executor = _register_dynamic_tool(hostile_name)
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", hostile_name, "{}"),
            _assistant_text("done"),
        ]
    )
    with caplog.at_level(logging.INFO, logger="ai4ia_api.agents.runtime"):
        result = await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=[hostile_name],
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ToolContext(),
        )
    # The tool genuinely dispatches/executes -- registry/handler membership
    # still governs authorization/routing, so this is a success, not a denial.
    tool_step = next(s for s in result.steps if s.kind == "tool_result")
    assert tool_step.result == {"ok": True}
    assert tool_step.tool == "unknown_tool"
    # The raw hostile name (and its embedded forged log line) never reaches any
    # log record or persisted step, despite being a real, registered tool name.
    assert all(hostile_name not in r.getMessage() for r in caplog.records)
    assert all("admin_backdoor" not in r.getMessage() for r in caplog.records)
    assert all(s.tool != hostile_name for s in result.steps)


async def test_two_malicious_registered_names_do_not_collide_in_dispatch(caplog):
    """Two distinct dynamically-registered tools with different hostile names
    both share the ``"unknown_tool"`` *log/activity* sentinel, but dispatch
    itself is keyed on the raw (unsentineled) name throughout, so each call
    still authorizes and executes its own, independent handler -- the shared
    sentinel is a logging/persistence-only concern and never causes one tool's
    call to be routed to, authorized as, or confused with the other's."""
    name_a = "weather\nINFO forged: tool=backdoor_a"
    name_b = "search\nINFO forged: tool=backdoor_b"
    registry = ToolRegistry()
    executor = ToolExecutor()
    for name, marker in ((name_a, "result-a"), (name_b, "result-b")):
        spec = ToolSpec(name=name, description="a dynamically registered tool")
        registry.register(spec)
        executor.register(
            ToolDefinition(
                spec=spec,
                parameters={"type": "object", "properties": {}},
                handler=lambda args, ctx, marker=marker: {"marker": marker},
            )
        )
    gateway = ScriptedGateway(
        [
            _assistant_tool_call("c1", name_a, "{}"),
            _assistant_tool_call("c2", name_b, "{}"),
            _assistant_text("done"),
        ]
    )
    with caplog.at_level(logging.INFO, logger="ai4ia_api.agents.runtime"):
        result = await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=[name_a, name_b],
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ToolContext(),
        )
    tool_results = [s for s in result.steps if s.kind == "tool_result"]
    assert {r.result["marker"] for r in tool_results} == {"result-a", "result-b"}
    # Both are sentineled identically for logging/activity purposes...
    assert all(s.tool == "unknown_tool" for s in tool_results)
    # ...but dispatch never conflated them: each produced its own distinct result.
    assert len(tool_results) == 2
    assert all(name_a not in r.getMessage() and name_b not in r.getMessage() for r in caplog.records)


async def test_gateway_failure_exposes_redacted_partial_result_after_tool_execution():
    """Iteration-two failure must not erase iteration one's irreversible work."""
    raw_argument = "argument-secret"
    raw_result = "result-secret"
    executed: list[dict] = []
    registry = ToolRegistry()
    executor = ToolExecutor()

    def handler(args, _ctx):
        executed.append(dict(args))
        return {"ok": True, "token": raw_result}

    definition = ToolDefinition(
        spec=ToolSpec(name="side_effect", description="records a side effect"),
        parameters={
            "type": "object",
            "properties": {"authorization": {"type": "string"}},
            "required": ["authorization"],
        },
        handler=handler,
    )
    registry.register(definition.spec)
    executor.register(definition)

    first = _assistant_tool_call(
        "c1",
        "side_effect",
        json.dumps({"authorization": raw_argument}),
    )
    first["choices"][0]["message"]["content"] = "Working. "
    first["usage"] = _usage(11, 4)

    class FailingSecondIterationGateway:
        def __init__(self) -> None:
            self.calls = 0
            self.failure = ModelGatewayError(502, "second iteration failed")

        async def stream(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                async for chunk in stream_like_gateway(first):
                    yield chunk
                return
            raise self.failure
            yield  # pragma: no cover

    gateway = FailingSecondIterationGateway()
    emitted: list[str] = []

    async def on_delta(text: str) -> None:
        emitted.append(text)

    with pytest.raises(Exception) as excinfo:
        await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=["side_effect"],
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ToolContext(),
            on_delta=on_delta,
        )

    failure = excinfo.value
    assert failure.__class__.__name__ == "AgentRunFailed"
    partial = failure.partial
    assert {
        "cause": failure.cause,
        "gateway_calls": gateway.calls,
        "executed": executed,
        "streamed": "".join(emitted),
        "partial_streamed": partial.streamed_text,
        "iterations": partial.iterations,
        "usage": (
            partial.usage.prompt,
            partial.usage.completion,
            partial.usage.total,
            partial.usage.calls,
            partial.usage.complete,
        ),
        "steps": [
            (step.kind, step.tool, step.arguments, step.result)
            for step in partial.steps
        ],
    } == {
        "cause": gateway.failure,
        "gateway_calls": 2,
        "executed": [{"authorization": raw_argument}],
        "streamed": "Working. ",
        "partial_streamed": "Working. ",
        "iterations": 2,
        "usage": (11, 4, 15, 2, False),
        "steps": [
            (
                "tool_result",
                "side_effect",
                {"authorization": "***REDACTED***"},
                {"ok": True, "token": "***REDACTED***"},
            )
        ],
    }
    assert raw_argument not in repr(partial.steps)
    assert raw_result not in repr(partial.steps)


async def test_cancellation_is_not_wrapped_as_agent_run_failure():
    class CancelledGateway:
        async def complete(self, **_kwargs):
            raise asyncio.CancelledError()

    registry, executor = build_tools()

    with pytest.raises(asyncio.CancelledError):
        await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=[],
            gateway=CancelledGateway(),
            registry=registry,
            executor=executor,
            ctx=ToolContext(),
        )


async def test_first_model_failure_without_partial_work_is_not_wrapped():
    failure = ModelGatewayError(502, "first call failed")

    class FirstCallFailureGateway:
        async def complete(self, **_kwargs):
            raise failure

    registry, executor = build_tools()
    with pytest.raises(ModelGatewayError) as excinfo:
        await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=[],
            gateway=FirstCallFailureGateway(),
            registry=registry,
            executor=executor,
            ctx=ToolContext(),
        )

    assert excinfo.value is failure
    assert issubclass(AgentRunFailed, RuntimeError)


async def test_first_failed_stream_with_observed_usage_is_partial_but_unknown():
    failure = ModelGatewayError(502, "failed after usage")

    class UsageThenFailureGateway:
        async def stream(self, **_kwargs):
            yield ChatChunk(
                usage={
                    "prompt_tokens": 9,
                    "completion_tokens": 0,
                    "total_tokens": 9,
                }
            )
            raise failure

    async def on_delta(_text: str) -> None:  # pragma: no cover
        raise AssertionError("no delta expected")

    registry, executor = build_tools()
    with pytest.raises(AgentRunFailed) as excinfo:
        await run_agent_turn(
            deployment="dep",
            messages=_messages(),
            tool_names=[],
            gateway=UsageThenFailureGateway(),
            registry=registry,
            executor=executor,
            ctx=ToolContext(),
            on_delta=on_delta,
        )

    usage = excinfo.value.partial.usage
    assert excinfo.value.cause is failure
    assert (usage.prompt, usage.completion, usage.total) == (9, 0, 9)
    assert (usage.calls, usage.known, usage.complete) == (1, True, True)
