"""Translate AI4IA's chat-completions contract to Claude Messages.

The rest of the application deliberately speaks one internal shape: OpenAI-style
messages, function tools, streamed deltas, and usage. Claude deployments in
Microsoft Foundry expose only ``/anthropic/v1/messages``, so this module is the
single provider boundary in both directions.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


ANTHROPIC_API = "anthropic"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise ValueError("message content must be text or a list of content blocks")

    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("message content blocks must be objects")
        block_type = block.get("type")
        if block_type in {"text", "input_text"}:
            text = block.get("text")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            continue
        raise ValueError(f"unsupported Claude input content block: {block_type!r}")
    return blocks


def _append_message(
    messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]
) -> None:
    if not blocks:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
        return
    messages.append({"role": role, "content": blocks})


def _tool_input(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function")
    if not isinstance(function, dict):
        raise ValueError("tool call is missing its function")
    raw = function.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # The runtime has already produced an invalid_arguments tool result.
            # Claude still needs the preceding tool_use block to keep the protocol
            # valid, but the malformed bytes cannot be represented as its object input.
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def messages_to_anthropic(
    messages: Sequence[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role")
        if role == "system":
            for block in _content_blocks(message.get("content", "")):
                system_parts.append(block["text"])
            continue

        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("tool result is missing tool_call_id")
            _append_message(
                converted,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": str(message.get("content") or ""),
                    }
                ],
            )
            continue

        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported Claude message role: {role!r}")

        blocks = _content_blocks(message.get("content", ""))
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    raise ValueError("assistant tool calls must be objects")
                function = call.get("function")
                call_id = call.get("id")
                name = function.get("name") if isinstance(function, dict) else None
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("assistant tool call is missing its id")
                if not isinstance(name, str) or not name:
                    raise ValueError("assistant tool call is missing its function name")
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": _tool_input(call),
                    }
                )
        _append_message(converted, role, blocks)

    system = "\n\n".join(system_parts) if system_parts else None
    return system, converted


def _tools_to_anthropic(raw_tools: Any) -> list[dict[str, Any]]:
    if raw_tools is None:
        return []
    if not isinstance(raw_tools, list):
        raise ValueError("tools must be a list")
    tools: list[dict[str, Any]] = []
    for raw in raw_tools:
        function = raw.get("function") if isinstance(raw, dict) else None
        if not isinstance(function, dict):
            raise ValueError("Claude tools must use the function-tool schema")
        name = function.get("name")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not name:
            raise ValueError("tool schema is missing its function name")
        if not isinstance(parameters, dict):
            raise ValueError(f"tool {name!r} is missing its JSON input schema")
        tool: dict[str, Any] = {"name": name, "input_schema": parameters}
        description = function.get("description")
        if isinstance(description, str) and description:
            tool["description"] = description
        tools.append(tool)
    return tools


def _tool_choice_to_anthropic(value: Any) -> dict[str, Any] | None:
    if value in (None, "auto"):
        return {"type": "auto"}
    if value == "required":
        return {"type": "any"}
    if value == "none":
        return None
    if isinstance(value, dict):
        function = value.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            return {"type": "tool", "name": name}
    raise ValueError("unsupported tool_choice for Claude Messages")


def build_anthropic_payload(
    *,
    deployment: str,
    messages: Sequence[dict[str, Any]],
    params: dict[str, Any] | None,
    stream: bool,
) -> dict[str, Any]:
    """Build a strict Claude Messages body from trusted internal chat inputs."""
    source = dict(params or {})
    system, converted = messages_to_anthropic(messages)
    try:
        max_tokens = max(1, int(source.get("max_tokens", DEFAULT_MAX_TOKENS)))
    except (TypeError, ValueError):
        max_tokens = DEFAULT_MAX_TOKENS

    body: dict[str, Any] = {
        "model": deployment,
        "messages": converted,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if system:
        body["system"] = system

    tools = _tools_to_anthropic(source.get("tools"))
    if tools:
        choice = _tool_choice_to_anthropic(source.get("tool_choice"))
        if choice is not None:
            body["tools"] = tools
            if source.get("parallel_tool_calls") is False:
                choice["disable_parallel_tool_use"] = True
            body["tool_choice"] = choice

    # Claude Opus 4.8 v2 rejects temperature and requires top_p=0.99. Omitting
    # both selects that provider default and avoids pretending the generic chat
    # sliders are portable. Provider-specific adaptive-thinking controls remain
    # on the model default until they have been probed through the live gateway.
    return body


def anthropic_usage_to_chat(usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None

    def token(name: str) -> int:
        value = usage.get(name)
        return int(value) if isinstance(value, (int, float)) else 0

    prompt = (
        token("input_tokens")
        + token("cache_creation_input_tokens")
        + token("cache_read_input_tokens")
    )
    completion = token("output_tokens")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def anthropic_json_to_chat(payload: dict[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            call_id = block.get("id")
            name = block.get("name")
            if isinstance(call_id, str) and isinstance(name, str):
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(
                                block.get("input") or {},
                                separators=(",", ":"),
                                ensure_ascii=True,
                            ),
                        },
                    }
                )

    stop_reason = payload.get("stop_reason")
    finish_reason = (
        {
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "end_turn": "stop",
            "stop_sequence": "stop",
        }.get(stop_reason, stop_reason)
        if isinstance(stop_reason, str)
        else None
    )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    result: dict[str, Any] = {
        "choices": [{"message": message, "finish_reason": finish_reason}]
    }
    usage = anthropic_usage_to_chat(payload.get("usage"))
    if usage is not None:
        result["usage"] = usage
    return result


@dataclass
class AnthropicStreamState:
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    saw_usage: bool = False

    def update(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        self.saw_usage = True
        for name in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        ):
            value = usage.get(name)
            if isinstance(value, (int, float)):
                setattr(self, name, max(getattr(self, name), int(value)))

    def as_chat_usage(self) -> dict[str, Any] | None:
        if not self.saw_usage:
            return None
        return anthropic_usage_to_chat(
            {
                "input_tokens": self.input_tokens,
                "cache_creation_input_tokens": self.cache_creation_input_tokens,
                "cache_read_input_tokens": self.cache_read_input_tokens,
                "output_tokens": self.output_tokens,
            }
        )


@dataclass
class AnthropicStreamEvent:
    delta: str = ""
    raw: str = ""
    usage: dict[str, Any] | None = None
    done: bool = False
    error: bool = False


def _chat_raw(delta: dict[str, Any]) -> str:
    return json.dumps(
        {"choices": [{"delta": delta}]},
        separators=(",", ":"),
        ensure_ascii=True,
    )


def parse_anthropic_event(
    payload: str, state: AnthropicStreamState
) -> AnthropicStreamEvent | None:
    if not payload:
        return None
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None

    event_type = event.get("type")
    if event_type == "error":
        return AnthropicStreamEvent(error=True)
    if event_type == "message_start":
        state.update((event.get("message") or {}).get("usage"))
        return None
    if event_type == "message_delta":
        state.update(event.get("usage"))
        return AnthropicStreamEvent(usage=state.as_chat_usage())
    if event_type == "message_stop":
        return AnthropicStreamEvent(done=True, usage=state.as_chat_usage())
    if event_type == "content_block_start":
        index = event.get("index")
        block = event.get("content_block") or {}
        if block.get("type") == "text":
            text = block.get("text") or ""
            if isinstance(text, str) and text:
                return AnthropicStreamEvent(
                    delta=text, raw=_chat_raw({"content": text})
                )
            return None
        if block.get("type") == "tool_use":
            initial = block.get("input")
            arguments = (
                json.dumps(initial, separators=(",", ":"), ensure_ascii=True)
                if isinstance(initial, dict) and initial
                else ""
            )
            return AnthropicStreamEvent(
                raw=_chat_raw(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": block.get("id"),
                                "type": "function",
                                "function": {
                                    "name": block.get("name") or "",
                                    "arguments": arguments,
                                },
                            }
                        ]
                    }
                )
            )
        return None
    if event_type == "content_block_delta":
        index = event.get("index")
        delta = event.get("delta") or {}
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = delta.get("text") or ""
            if isinstance(text, str) and text:
                return AnthropicStreamEvent(
                    delta=text, raw=_chat_raw({"content": text})
                )
        elif delta_type == "input_json_delta":
            partial = delta.get("partial_json") or ""
            if isinstance(partial, str) and partial:
                return AnthropicStreamEvent(
                    raw=_chat_raw(
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "function": {"arguments": partial},
                                }
                            ]
                        }
                    )
                )
    return None
