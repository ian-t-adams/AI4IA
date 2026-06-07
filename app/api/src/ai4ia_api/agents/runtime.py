"""Agent **runtime** (Phase 4): a gateway-native tool-calling loop.

Given a fully-built message list and an agent's allowlisted tool names, this runs
the standard tool-calling protocol against the model gateway:

  1. Call the model with the agent's authorized tool schema.
  2. If the model returns ``tool_calls``, append the assistant message verbatim,
     then for EACH call emit exactly one ``role:"tool"`` result (success,
     validation error, authorization denial, or execution error) keyed by
     ``tool_call_id`` — never leaving a tool call unanswered.
  3. Loop (bounded) until the model returns a plain answer, then return it.

Governance is centralized: every invocation is re-checked against the tool-safety
registry (defense in depth on top of schema-time filtering), and all arguments,
results, and error strings are redacted before they enter the step trace or logs.
The real Microsoft Agent Framework / Foundry toolbox / MCP can later replace the
:class:`~ai4ia_api.agents.tool_exec.ToolExecutor` behind this same loop.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..gateway.client import ModelGatewayClient
from .tool_exec import ToolContext, ToolExecutor, ToolValidationError
from .tools import ToolRegistry, redact, redact_obj

logger = logging.getLogger(__name__)

# Per-turn safety bounds. ``max_iters`` caps model<->tool round trips; the call
# budget caps total tool invocations across the whole turn (one iteration may ask
# for several tools); result truncation caps how much a single tool can inject
# back into the context window.
_DEFAULT_MAX_ITERS = 4
_MAX_TOOL_CALLS = 8
_MAX_TOOL_RESULT_BYTES = 8192

# Caller params the governed turn always controls itself.
_RESERVED_PARAMS = ("tools", "tool_choice", "parallel_tool_calls")


@dataclass
class AgentStep:
    """One redacted entry in the agent's execution trace."""

    kind: str  # tool_result | tool_denied | tool_error | final
    tool: str | None = None
    arguments: Any = None
    result: Any = None
    detail: str | None = None


@dataclass
class AgentRunResult:
    text: str
    model: str
    steps: list[AgentStep] = field(default_factory=list)
    iterations: int = 0


def _tool_message(call_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload)}


def _truncate(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= _MAX_TOOL_RESULT_BYTES:
        return text
    return encoded[:_MAX_TOOL_RESULT_BYTES].decode("utf-8", "ignore") + "...[truncated]"


def _final_text(result: dict[str, Any]) -> str:
    message = (result.get("choices") or [{}])[0].get("message") or {}
    return message.get("content") or ""


async def run_agent_turn(
    *,
    deployment: str,
    messages: Sequence[dict[str, Any]],
    tool_names: Sequence[str],
    gateway: ModelGatewayClient,
    registry: ToolRegistry,
    executor: ToolExecutor,
    ctx: ToolContext,
    params: dict[str, Any] | None = None,
    max_iters: int = _DEFAULT_MAX_ITERS,
) -> AgentRunResult:
    """Run a single agent turn with tool calling and return the final answer."""
    convo: list[dict[str, Any]] = [dict(m) for m in messages]
    schema = executor.schema_for(tool_names, registry=registry, ctx=ctx)
    steps: list[AgentStep] = []
    denied_once: set[str] = set()
    tool_calls_used = 0

    base_params = dict(params or {})
    for key in _RESERVED_PARAMS:
        base_params.pop(key, None)

    iterations = 0
    while iterations < max_iters:
        iterations += 1
        req_params = dict(base_params)
        if schema:
            req_params["tools"] = schema
            req_params["tool_choice"] = "auto"
        result = await gateway.complete(
            deployment=deployment,
            messages=convo,
            params=req_params,
            correlation_id=ctx.correlation_id,
        )
        message = (result.get("choices") or [{}])[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            steps.append(AgentStep(kind="final"))
            return AgentRunResult(
                text=message.get("content") or "",
                model=deployment,
                steps=steps,
                iterations=iterations,
            )

        # Preserve the assistant tool-call message verbatim (content may be null)
        # so the subsequent tool results reference valid call ids.
        convo.append(
            {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": tool_calls,
            }
        )

        force_final = False
        for call in tool_calls:
            call_id = call.get("id")
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"

            tool_calls_used += 1
            if tool_calls_used > _MAX_TOOL_CALLS:
                convo.append(
                    _tool_message(
                        call_id,
                        {"error": {"type": "budget_exceeded", "message": "tool budget exhausted"}},
                    )
                )
                steps.append(AgentStep(kind="tool_error", tool=name, detail="budget_exceeded"))
                force_final = True
                continue

            try:
                parsed = json.loads(raw_args) if str(raw_args).strip() else {}
                if not isinstance(parsed, dict):
                    raise ValueError("arguments must be a JSON object")
            except (json.JSONDecodeError, ValueError) as exc:
                convo.append(
                    _tool_message(
                        call_id,
                        {"error": {"type": "invalid_arguments", "message": redact(str(exc))}},
                    )
                )
                steps.append(AgentStep(kind="tool_error", tool=name, detail="invalid_arguments"))
                continue

            decision = registry.authorize(
                name,
                granted_scopes=ctx.granted_scopes,
                target_hosts=ctx.target_hosts,
                approved=name in ctx.approvals,
            )
            if not decision.allowed:
                reason = decision.reason.value if decision.reason else "denied"
                convo.append(
                    _tool_message(
                        call_id,
                        {"error": {"type": "authorization_denied", "message": reason}},
                    )
                )
                steps.append(AgentStep(kind="tool_denied", tool=name, detail=reason))
                logger.info("agent tool denied: tool=%s reason=%s", name, reason)
                # A second denial of the same tool means the model is looping;
                # cut tools off so the next call must produce a final answer.
                if name in denied_once:
                    force_final = True
                denied_once.add(name)
                continue

            try:
                raw_result = await executor.execute(name, parsed, ctx)
            except ToolValidationError as exc:
                convo.append(
                    _tool_message(
                        call_id,
                        {"error": {"type": "validation_error", "message": redact(str(exc))}},
                    )
                )
                steps.append(
                    AgentStep(
                        kind="tool_error",
                        tool=name,
                        arguments=redact_obj(parsed),
                        detail="validation_error",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - surface as a tool result, never crash the turn
                convo.append(
                    _tool_message(
                        call_id,
                        {"error": {"type": "execution_error", "message": redact(str(exc))}},
                    )
                )
                steps.append(
                    AgentStep(
                        kind="tool_error",
                        tool=name,
                        arguments=redact_obj(parsed),
                        detail="execution_error",
                    )
                )
                logger.warning("agent tool error: tool=%s", name)
                continue

            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _truncate(json.dumps(raw_result, default=str)),
                }
            )
            steps.append(
                AgentStep(
                    kind="tool_result",
                    tool=name,
                    arguments=redact_obj(parsed),
                    result=redact_obj(raw_result),
                )
            )
            logger.info("agent tool ran: tool=%s args=%s", name, redact_obj(parsed))

        if force_final:
            schema = []  # disable tools so the next call yields a natural answer

    # Iterations exhausted: take one final answer with tools disabled so the model
    # must respond in natural language rather than request yet another tool.
    final = await gateway.complete(
        deployment=deployment,
        messages=convo,
        params=base_params,
        correlation_id=ctx.correlation_id,
    )
    steps.append(AgentStep(kind="final", detail="max_iters"))
    return AgentRunResult(
        text=_final_text(final),
        model=deployment,
        steps=steps,
        iterations=iterations,
    )
