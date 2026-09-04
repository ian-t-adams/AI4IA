"""Tests for the streamed tool loop (audit finding P1-16).

Two layers:

* :class:`ToolCallAccumulator` / :func:`stream_iteration` in isolation — the
  reassembly of a ``tool_calls`` array from index-keyed SSE fragments, which is
  where a streamed tool loop actually goes wrong. Every case here is a wire shape
  a real provider emits, not a synthetic one.
* :func:`run_agent_turn` end-to-end over that transport — proving the governed
  loop behaves identically when it is fed fragments instead of whole objects, and
  that the non-streaming path is still taken when it should be.
"""
from __future__ import annotations

import json

import pytest

from ai4ia_api.agents.runtime import run_agent_turn
from ai4ia_api.agents.streaming import (
    ToolCallAccumulator,
    _MAX_ARGUMENT_CHARS,
    stream_iteration,
)
from ai4ia_api.agents.tool_exec import ToolContext, build_tools
from ai4ia_api.gateway.client import ChatChunk, ModelGatewayError


def _fragments(*fragments: dict) -> list[dict]:
    return list(fragments)


# --- ToolCallAccumulator -----------------------------------------------------


def test_arguments_split_across_chunks_are_reassembled():
    """The split lands mid-escape, so no fragment is independently parseable."""
    acc = ToolCallAccumulator()
    acc.add(
        _fragments(
            {
                "index": 0,
                "id": "call_a",
                "type": "function",
                "function": {"name": "web_search", "arguments": ""},
            }
        )
    )
    for piece in ('{"query": "sarah\\', 'u2019s repo", "top', '": 3}'):
        acc.add(_fragments({"index": 0, "function": {"arguments": piece}}))

    calls = acc.finalize()
    assert len(calls) == 1
    assert calls[0]["id"] == "call_a"
    assert calls[0]["function"]["name"] == "web_search"
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "query": "sarah\u2019s repo",
        "top": 3,
    }


def test_parallel_calls_are_keyed_by_index_not_arrival_order():
    """Interleaved fragments for two slots, with slot 1 opened first."""
    acc = ToolCallAccumulator()
    acc.add(_fragments({"index": 1, "id": "b", "function": {"name": "news_search"}}))
    acc.add(_fragments({"index": 0, "id": "a", "function": {"name": "web_search"}}))
    acc.add(_fragments({"index": 1, "function": {"arguments": '{"q":"b'}}))
    acc.add(_fragments({"index": 0, "function": {"arguments": '{"q":"a'}}))
    acc.add(_fragments({"index": 1, "function": {"arguments": '"}'}}))
    acc.add(_fragments({"index": 0, "function": {"arguments": '"}'}}))

    calls = acc.finalize()
    assert [c["id"] for c in calls] == ["a", "b"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"q": "a"}
    assert json.loads(calls[1]["function"]["arguments"]) == {"q": "b"}


def test_name_may_arrive_in_fragments():
    acc = ToolCallAccumulator()
    acc.add(_fragments({"index": 0, "id": "a", "function": {"name": "web_"}}))
    acc.add(_fragments({"index": 0, "function": {"name": "search", "arguments": "{}"}}))
    assert acc.finalize()[0]["function"]["name"] == "web_search"


def test_a_later_empty_id_never_erases_the_real_one():
    acc = ToolCallAccumulator()
    acc.add(_fragments({"index": 0, "id": "call_a", "type": "function",
                        "function": {"name": "calculator"}}))
    acc.add(_fragments({"index": 0, "id": "", "type": "", "function": {"arguments": "{}"}}))
    call = acc.finalize()[0]
    assert call["id"] == "call_a" and call["type"] == "function"


def test_a_later_different_id_never_hijacks_an_established_slot():
    """One slot, one identity: the first non-empty ``id`` wins outright.

    The same guard covers both this and the empty-id case above, which is why
    both are asserted — a guard tested from one side only is half-tested.
    """
    acc = ToolCallAccumulator()
    acc.add(_fragments({"index": 0, "id": "call_a", "function": {"name": "calculator"}}))
    acc.add(_fragments({"index": 0, "id": "call_b", "function": {"arguments": "{}"}}))
    calls = acc.finalize()
    assert len(calls) == 1 and calls[0]["id"] == "call_a"


def test_a_bare_continuation_extends_the_open_call():
    """Some providers drop ``index`` on continuation fragments entirely."""
    acc = ToolCallAccumulator()
    acc.add(_fragments({"index": 0, "id": "a", "function": {"name": "calculator"}}))
    acc.add(_fragments({"function": {"arguments": '{"expression":'}}))
    acc.add(_fragments({"function": {"arguments": '"6*7"}'}}))
    assert json.loads(acc.finalize()[0]["function"]["arguments"]) == {"expression": "6*7"}


def test_fragments_keyed_only_by_id_still_merge():
    acc = ToolCallAccumulator()
    acc.add(_fragments({"id": "a", "function": {"name": "calculator", "arguments": '{"e'}}))
    acc.add(_fragments({"id": "a", "function": {"arguments": 'xpression":"1"}'}}))
    calls = acc.finalize()
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"expression": "1"}


def test_malformed_fragments_are_skipped_rather_than_raised():
    """This runs in the one path every chat request takes: never raise."""
    acc = ToolCallAccumulator()
    acc.add("not a list")  # type: ignore[arg-type]
    acc.add(_fragments({"index": 0, "id": "a", "function": {"name": "calculator"}}))
    acc.add([None, 42, {"index": 0, "function": "not a dict"}])  # type: ignore[list-item]
    acc.add(_fragments({"index": 0, "function": {"arguments": "{}"}}))
    calls = acc.finalize()
    assert len(calls) == 1 and calls[0]["function"]["name"] == "calculator"


def test_a_true_index_is_not_confused_with_a_boolean():
    """``True == 1`` in Python; a bool must not be read as slot 1."""
    acc = ToolCallAccumulator()
    acc.add(_fragments({"index": 1, "id": "b", "function": {"name": "news_search"}}))
    acc.add(_fragments({"index": True, "id": "c", "function": {"name": "web_search"}}))
    assert {c["id"] for c in acc.finalize()} == {"b", "c"}


def test_oversized_arguments_are_truncated_rather_than_buffered_without_bound():
    acc = ToolCallAccumulator()
    acc.add(_fragments({"index": 0, "id": "a", "function": {"name": "calculator"}}))
    for _ in range(4):
        acc.add(_fragments({"index": 0, "function": {"arguments": "x" * (_MAX_ARGUMENT_CHARS // 2)}}))
    arguments = acc.finalize()[0]["function"]["arguments"]
    assert len(arguments) == _MAX_ARGUMENT_CHARS
    # Truncated => unparseable => the runtime's existing structured
    # ``invalid_arguments`` tool result, rather than a crash or a silent call.
    with pytest.raises(json.JSONDecodeError):
        json.loads(arguments)


def test_a_slot_that_never_carried_a_call_is_dropped():
    acc = ToolCallAccumulator()
    acc.add(_fragments({"index": 0, "id": "a", "type": "function", "function": {}}))
    assert acc.finalize() == []


# --- stream_iteration --------------------------------------------------------


class _ChunkGateway:
    """Yields a fixed ChatChunk script; records the params it was called with."""

    def __init__(self, chunks: list[ChatChunk]) -> None:
        self._chunks = chunks
        self.params: list[dict] = []

    async def stream(self, *, deployment, messages, params=None, correlation_id=None):
        self.params.append(dict(params or {}))
        for chunk in self._chunks:
            yield chunk


def _text_chunk(piece: str) -> ChatChunk:
    return ChatChunk(delta=piece, raw=json.dumps({"choices": [{"delta": {"content": piece}}]}))


def _call_chunk(fragment: dict) -> ChatChunk:
    return ChatChunk(raw=json.dumps({"choices": [{"delta": {"tool_calls": [fragment]}}]}))


async def test_text_is_forwarded_increment_by_increment_with_usage_and_calls():
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    gateway = _ChunkGateway(
        [
            _text_chunk("Let me "),
            _text_chunk("look."),
            _call_chunk({"index": 0, "id": "a", "function": {"name": "web_search", "arguments": ""}}),
            _call_chunk({"index": 0, "function": {"arguments": '{"query":"x"}'}}),
            ChatChunk(usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}),
            ChatChunk(done=True, raw="[DONE]"),
        ]
    )
    iteration = await stream_iteration(
        gateway=gateway,
        deployment="dep",
        messages=[{"role": "user", "content": "hi"}],
        params={"tools": []},
        correlation_id="corr",
        on_delta=on_delta,
    )

    assert seen == ["Let me ", "look."]
    assert iteration.content == "Let me look."
    assert iteration.usage == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert json.loads(iteration.tool_calls[0]["function"]["arguments"]) == {"query": "x"}


async def test_an_error_frame_raises_a_sanitized_gateway_error():
    """The plain streaming path never echoes upstream detail; nor may this one."""
    gateway = _ChunkGateway(
        [
            _text_chunk("partial"),
            ChatChunk(raw=json.dumps({"error": {"message": "internal deployment secret"}})),
        ]
    )
    with pytest.raises(ModelGatewayError) as excinfo:
        await stream_iteration(
            gateway=gateway,
            deployment="dep",
            messages=[],
            params=None,
            correlation_id=None,
        )
    assert excinfo.value.detail == "Model stream failed."
    assert "internal deployment secret" not in str(excinfo.value)


async def test_eof_without_terminal_event_is_a_stream_failure():
    gateway = _ChunkGateway([_text_chunk("partial")])

    with pytest.raises(ModelGatewayError) as excinfo:
        await stream_iteration(
            gateway=gateway,
            deployment="dep",
            messages=[],
            params=None,
            correlation_id=None,
        )

    assert excinfo.value.detail == "Model stream failed."


# --- run_agent_turn over the streamed transport ------------------------------


class _StreamingScriptedGateway:
    """Replays scripted iterations as SSE, fragmenting every tool call."""

    def __init__(self, script: list[tuple[str, list[dict]]]) -> None:
        self._script = list(script)
        self.iterations = 0
        self.completes = 0

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.completes += 1
        text, calls = self._script.pop(0) if self._script else ("(exhausted)", [])
        message: dict = {"role": "assistant", "content": text or None}
        if calls:
            message["tool_calls"] = calls
        return {"choices": [{"message": message}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None):
        self.iterations += 1
        text, calls = self._script.pop(0) if self._script else ("(exhausted)", [])
        for index in range(0, len(text), 4):
            yield _text_chunk(text[index : index + 4])
        for index, call in enumerate(calls):
            function = call["function"]
            yield _call_chunk(
                {
                    "index": index,
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": function["name"], "arguments": ""},
                }
            )
            arguments = function["arguments"]
            for offset in range(0, len(arguments), 3):
                yield _call_chunk(
                    {"index": index, "function": {"arguments": arguments[offset : offset + 3]}}
                )
        yield ChatChunk(done=True, raw="[DONE]")


def _calculator_call(call_id: str, expression: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "calculator", "arguments": json.dumps({"expression": expression})},
    }


async def test_a_streamed_turn_forwards_text_and_still_runs_the_tool():
    registry, executor = build_tools()
    gateway = _StreamingScriptedGateway(
        [
            ("Working on it. ", [_calculator_call("c1", "6*7")]),
            ("It is 42.", []),
        ]
    )
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    result = await run_agent_turn(
        deployment="dep",
        messages=[{"role": "user", "content": "6*7?"}],
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        on_delta=on_delta,
    )

    # Both iterations streamed, and nothing took the non-streaming path.
    assert gateway.iterations == 2 and gateway.completes == 0
    # More than one increment, which is the whole point: a single terminal blob
    # would satisfy the concatenation check while reproducing the defect.
    assert len(seen) > 1
    assert "".join(seen) == "Working on it. It is 42."
    assert result.streamed_text == "Working on it. It is 42."
    # ``text`` is still the FINAL iteration only; the preamble lives in
    # ``streamed_text`` because that is what the user actually received.
    assert result.text == "It is 42."
    # The fragmented arguments were reassembled well enough to reach the real
    # executor and produce a real result.
    tool_step = next(s for s in result.steps if s.kind == "tool_result")
    assert tool_step.tool == "calculator"
    assert tool_step.result == {"expression": "6*7", "result": 42}


async def test_without_on_delta_the_turn_never_streams():
    """The kill switch and every non-streaming caller depend on this."""
    registry, executor = build_tools()

    class _NoStream(_StreamingScriptedGateway):
        async def stream(self, **_kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("a turn with no on_delta must not stream")
            yield  # pragma: no cover

    gateway = _NoStream([("It is 42.", [])])
    result = await run_agent_turn(
        deployment="dep",
        messages=[{"role": "user", "content": "hi"}],
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
    )
    assert result.text == "It is 42." and gateway.completes == 1
    assert result.streamed_text == ""


async def test_a_gateway_with_no_stream_falls_back_rather_than_raising():
    """Workflow runners and delegation sub-turns inject exactly such a gateway."""
    registry, executor = build_tools()

    class _CompleteOnly:
        def __init__(self) -> None:
            self.completes = 0

        async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
            self.completes += 1
            return {"choices": [{"message": {"role": "assistant", "content": "plain"}}]}

    gateway = _CompleteOnly()
    seen: list[str] = []

    async def on_delta(text: str) -> None:  # pragma: no cover - never called
        seen.append(text)

    result = await run_agent_turn(
        deployment="dep",
        messages=[{"role": "user", "content": "hi"}],
        tool_names=[],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        on_delta=on_delta,
    )
    assert result.text == "plain" and gateway.completes == 1 and seen == []


async def test_the_per_turn_tool_budget_still_bounds_a_streamed_turn():
    registry, executor = build_tools()
    # Nine calls in one assistant message; the budget is eight.
    calls = [_calculator_call(f"c{i}", "1+1") for i in range(9)]
    gateway = _StreamingScriptedGateway([("", calls), ("done", [])])
    result = await run_agent_turn(
        deployment="dep",
        messages=[{"role": "user", "content": "go"}],
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        on_delta=lambda _text: _noop(),
    )
    details = [s.detail for s in result.steps if s.kind == "tool_error"]
    assert "budget_exceeded" in details
    assert sum(1 for s in result.steps if s.kind == "tool_result") == 8


async def _noop() -> None:
    return None


async def test_a_failing_delta_callback_never_breaks_the_turn():
    registry, executor = build_tools()
    gateway = _StreamingScriptedGateway([("It is 42.", [])])

    async def on_delta(_text: str) -> None:
        raise RuntimeError("the client went away")

    result = await run_agent_turn(
        deployment="dep",
        messages=[{"role": "user", "content": "hi"}],
        tool_names=[],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        on_delta=on_delta,
    )
    assert result.text == "It is 42."


async def test_an_unparseable_streamed_argument_becomes_a_structured_tool_result():
    """Reassembly can still yield invalid JSON; that must not crash the turn."""
    registry, executor = build_tools()
    broken = {
        "id": "c1",
        "type": "function",
        "function": {"name": "calculator", "arguments": '{"expression": '},
    }
    gateway = _StreamingScriptedGateway([("", [broken]), ("sorry", [])])
    result = await run_agent_turn(
        deployment="dep",
        messages=[{"role": "user", "content": "go"}],
        tool_names=["calculator"],
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        on_delta=lambda _text: _noop(),
    )
    assert [s.detail for s in result.steps if s.kind == "tool_error"] == ["invalid_arguments"]
    assert result.text == "sorry"
