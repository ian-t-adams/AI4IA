"""Shared conservative prompt budgeting for chat and tool-loop model calls."""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

FALLBACK_CONTEXT_WINDOW_TOKENS = 192_000
TOOL_CONTEXT_RESERVE_TOKENS = 4_096
MESSAGE_ENVELOPE_RESERVE_BYTES = 32


def prompt_byte_budget(
    context_window: int | None,
    params: dict[str, Any],
    *,
    default_max_tokens: int,
) -> int:
    """Prompt budget after reserving requested output and tool-loop headroom."""
    context = context_window or FALLBACK_CONTEXT_WINDOW_TOKENS
    requested_output = params.get("max_tokens")
    if isinstance(requested_output, (int, float, str)):
        try:
            output = max(1, int(requested_output))
        except ValueError:
            output = default_max_tokens
    else:
        output = default_max_tokens
    reserved = min(max(0, context - 1), output + TOOL_CONTEXT_RESERVE_TOKENS)
    return max(1, context - reserved)


def serialized_budget_bytes(value: Any) -> int:
    """Exact compact UTF-8 JSON size used for non-message payload structures."""
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    )


def message_budget_bytes(message: dict[str, Any]) -> int:
    """Conservative message size, including tool-call/result protocol fields."""
    extra = {key: value for key, value in message.items() if key not in {"role", "content"}}
    return (
        len(str(message.get("content") or "").encode("utf-8"))
        + MESSAGE_ENVELOPE_RESERVE_BYTES
        + (serialized_budget_bytes(extra) if extra else 0)
    )


def bound_payload_history(
    messages: Sequence[dict[str, Any]],
    *,
    prompt_budget_bytes: int,
    additional_fixed_bytes: int = 0,
) -> tuple[list[dict[str, Any]], int, int]:
    """Keep fixed system/current-turn content plus the newest complete prior turns.

    The last user message and everything after it form the current turn. Historical
    groups begin at a user message and include every following assistant/tool
    message up to the next user, so a tool call is never separated from its results.
    """
    if not messages:
        if additional_fixed_bytes > prompt_budget_bytes:
            raise ValueError("fixed prompt content exceeds the selected model budget")
        return [], 0, 0

    current_user_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "user"
        ),
        len(messages) - 1,
    )
    fixed_indexes = {
        index
        for index, message in enumerate(messages)
        if message.get("role") == "system" or index >= current_user_index
    }
    history_indexes = [
        index
        for index in range(current_user_index)
        if index not in fixed_indexes
    ]
    used = additional_fixed_bytes + sum(
        message_budget_bytes(messages[index]) for index in fixed_indexes
    )
    if used > prompt_budget_bytes:
        raise ValueError("fixed prompt content exceeds the selected model budget")

    history_groups: list[list[int]] = []
    for index in history_indexes:
        if messages[index].get("role") == "user" or not history_groups:
            history_groups.append([index])
        else:
            history_groups[-1].append(index)

    kept_history: set[int] = set()
    for group in reversed(history_groups):
        size = sum(message_budget_bytes(messages[index]) for index in group)
        if used + size > prompt_budget_bytes:
            break
        kept_history.update(group)
        used += size

    kept_indexes = fixed_indexes | kept_history
    bounded = [dict(message) for index, message in enumerate(messages) if index in kept_indexes]
    dropped = [messages[index] for index in history_indexes if index not in kept_history]
    return bounded, len(dropped), sum(message_budget_bytes(message) for message in dropped)


def bound_agent_context(
    messages: Sequence[dict[str, Any]],
    *,
    current_user_index: int,
    prompt_budget_bytes: int,
    additional_fixed_bytes: int = 0,
) -> tuple[list[dict[str, Any]], int, int]:
    """Evict old turns while keeping the entire in-progress agent turn atomic."""
    if not messages:
        return [], 0, 0
    if not 0 <= current_user_index < len(messages):
        raise ValueError("current user message is missing")
    if messages[current_user_index].get("role") != "user":
        raise ValueError("current user message is invalid")

    pending_tool_results: dict[str | None, int] = {}
    for message in messages[current_user_index + 1 :]:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            if pending_tool_results:
                raise ValueError("assistant tool call is missing results")
            for call in message["tool_calls"]:
                call_id = call.get("id")
                pending_tool_results[call_id] = pending_tool_results.get(call_id, 0) + 1
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if pending_tool_results.get(call_id, 0) <= 0:
                raise ValueError("tool result is missing its assistant call")
            pending_tool_results[call_id] -= 1
            if pending_tool_results[call_id] == 0:
                pending_tool_results.pop(call_id)
        else:
            raise ValueError("current agent turn contains an invalid message")
    if pending_tool_results:
        raise ValueError("assistant tool call is missing results")

    fixed = {
        index
        for index, message in enumerate(messages)
        if message.get("role") == "system" or index >= current_user_index
    }
    used = additional_fixed_bytes + sum(
        message_budget_bytes(messages[index]) for index in fixed
    )
    if used > prompt_budget_bytes:
        raise ValueError("fixed prompt content exceeds the selected model budget")

    history_indexes = [
        index
        for index in range(current_user_index)
        if index not in fixed
    ]
    history_groups: list[list[int]] = []
    for index in history_indexes:
        if messages[index].get("role") == "user" or not history_groups:
            history_groups.append([index])
        else:
            history_groups[-1].append(index)
    kept_history: set[int] = set()
    for group in reversed(history_groups):
        size = sum(message_budget_bytes(messages[index]) for index in group)
        if used + size > prompt_budget_bytes:
            break
        kept_history.update(group)
        used += size

    selected = fixed | kept_history
    return (
        [dict(message) for index, message in enumerate(messages) if index in selected],
        len(history_indexes) - len(kept_history),
        0,
    )
