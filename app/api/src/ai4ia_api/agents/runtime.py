"""Agent **runtime**: a gateway-native tool-calling loop.

Given a fully-built message list and an agent's allowlisted tool names, this runs
the standard tool-calling protocol against the model gateway:

  1. Call the model with the agent's authorized tool schema.
  2. If the model returns ``tool_calls``, append the assistant message verbatim,
     then for EACH call emit exactly one ``role:"tool"`` result (success,
     validation error, authorization denial, or execution error) keyed by
     ``tool_call_id`` — never leaving a tool call unanswered.
  3. Loop (bounded) until the model returns a plain answer, then return it.

Governance is centralized: every invocation is re-checked against the tool-safety
registry (defense in depth on top of schema-time filtering). Argument/result
content is only ever credential-redacted (``redact_obj``) before it lands on the
in-process ``AgentStep`` trace, and free-text log lines never include it at all —
only the tool name and fixed, bounded status/reason strings reach logs or the
user-facing activity view (see ``agents.activity``).
The real Microsoft Agent Framework / Foundry toolbox / MCP can later replace the
:class:`~ai4ia_api.agents.tool_exec.ToolExecutor` behind this same loop.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..gateway.client import ModelGatewayClient
from ..usage.models import TokenUsage
from ..logging_setup import emit_custom_event, emit_security_block
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

    kind: str  # tool_result | tool_denied | tool_error | delegate | final
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
    # Token usage summed across every model call in the turn. ``usage.complete``
    # is False if any call did not report usage, so cost is never overstated.
    usage: TokenUsage = field(default_factory=TokenUsage.empty)


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
    extra_tools: Sequence[dict[str, Any]] | None = None,
    extra_handlers: Mapping[str, Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]]
    | None = None,
    on_step: Callable[[AgentStep], Awaitable[None]] | None = None,
) -> AgentRunResult:
    """Run a single agent turn with tool calling and return the final answer.

    ``extra_tools``/``extra_handlers`` inject *synthetic* capabilities (e.g. the
    ``delegate_to_agent`` orchestration tool) on top of the registry-backed tools.
    Each synthetic tool is an OpenAI function schema in ``extra_tools`` plus an
    async handler in ``extra_handlers`` keyed by the function name. Synthetic
    names MUST be disjoint from the real executor tool names (asserted below, fail
    closed) so a synthetic capability can never shadow a governed tool. Synthetic
    handlers bypass the registry authorize/execute path but are still counted
    against the per-turn tool-call budget and are wrapped so an exception becomes
    a structured tool result rather than crashing the turn.
    """
    convo: list[dict[str, Any]] = [dict(m) for m in messages]
    real_schema = executor.schema_for(tool_names, registry=registry, ctx=ctx)
    handlers: dict[str, Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]] = (
        dict(extra_handlers) if extra_handlers else {}
    )
    if handlers:
        real_names = {t.get("function", {}).get("name") for t in real_schema}
        collisions = real_names & set(handlers)
        if collisions:
            raise ValueError(
                f"extra_handlers collide with executor tool names: {sorted(collisions)}"
            )
    schema = [*real_schema, *(extra_tools or [])]
    steps: list[AgentStep] = []
    denied_once: set[str] = set()
    tool_calls_used = 0

    async def record(step: AgentStep, *, persist: bool = True) -> None:
        """Append a finalized step to the trace and/or surface it live.

        ``persist=False`` is used for the pre-execution ``tool_start`` marker: it
        is streamed for the live activity indicator but kept out of the durable
        trace (which records only what actually happened). The callback is
        best-effort so a UI/stream error can never break the turn.
        """
        if persist:
            steps.append(step)
        if on_step is not None:
            try:
                await on_step(step)
            except Exception:  # noqa: BLE001 - a UI callback must never break a turn
                logger.debug("on_step callback failed", exc_info=True)

    base_params = dict(params or {})
    for key in _RESERVED_PARAMS:
        base_params.pop(key, None)

    usage_agg = TokenUsage.empty()
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
        usage_agg = usage_agg.add(TokenUsage.parse(result.get("usage")))
        message = (result.get("choices") or [{}])[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            await record(AgentStep(kind="final"))
            return AgentRunResult(
                text=message.get("content") or "",
                model=deployment,
                steps=steps,
                iterations=iterations,
                usage=usage_agg,
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
                await record(AgentStep(kind="tool_error", tool=name, detail="budget_exceeded"))
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
                await record(AgentStep(kind="tool_error", tool=name, detail="invalid_arguments"))
                continue

            # Emit a pre-execution marker so the UI can show "running X" live. It
            # is NOT persisted (only the finalized result/denied/error step is).
            await record(
                AgentStep(kind="tool_start", tool=name, arguments=redact_obj(parsed)),
                persist=False,
            )

            # Synthetic capabilities (e.g. delegate_to_agent) are dispatched here,
            # before the registry path. They are disjoint from real tool names
            # (asserted at setup), count against the shared tool-call budget, and
            # are wrapped so a handler error becomes a structured tool result.
            if name in handlers:
                try:
                    raw_result = await handlers[name](parsed, ctx)
                except Exception as exc:  # noqa: BLE001 - never crash the turn
                    convo.append(
                        _tool_message(
                            call_id,
                            {"error": {"type": "execution_error", "message": redact(str(exc))}},
                        )
                    )
                    await record(
                        AgentStep(
                            kind="tool_error",
                            tool=name,
                            arguments=redact_obj(parsed),
                            detail="execution_error",
                        )
                    )
                    logger.warning("agent delegate error: tool=%s", name)
                    continue
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": _truncate(json.dumps(raw_result, default=str)),
                    }
                )
                await record(
                    AgentStep(
                        kind="delegate",
                        tool=name,
                        arguments=redact_obj(parsed),
                        result=redact_obj(raw_result),
                    )
                )
                logger.info("agent delegated: tool=%s", name)
                continue

            decision = registry.authorize(
                name,
                granted_scopes=ctx.granted_scopes,
                target_hosts=ctx.target_hosts,
                approved=name in ctx.approvals,
            )
            if not decision.allowed:
                reason = decision.reason.value if decision.reason else "denied"
                emit_custom_event(
                    "tool_authorization",
                    {
                        "tool": name,
                        "source": "agent_runtime",
                        "outcome": "denied",
                        "reason": reason,
                    },
                )
                emit_security_block("tool_authorization", reason, "agent_runtime")
                convo.append(
                    _tool_message(
                        call_id,
                        {"error": {"type": "authorization_denied", "message": reason}},
                    )
                )
                await record(AgentStep(kind="tool_denied", tool=name, detail=reason))
                logger.info("agent tool denied: tool=%s reason=%s", name, reason)
                # A second denial of the same tool means the model is looping;
                # cut tools off so the next call must produce a final answer.
                if name in denied_once:
                    force_final = True
                denied_once.add(name)
                continue

            started = time.monotonic()
            try:
                raw_result = await executor.execute(name, parsed, ctx)
            except ToolValidationError as exc:
                emit_custom_event(
                    "tool_authorization",
                    {
                        "tool": name,
                        "source": "agent_runtime",
                        "outcome": "validation_error",
                        "latencyMs": int((time.monotonic() - started) * 1000),
                    },
                )
                convo.append(
                    _tool_message(
                        call_id,
                        {"error": {"type": "validation_error", "message": redact(str(exc))}},
                    )
                )
                await record(
                    AgentStep(
                        kind="tool_error",
                        tool=name,
                        arguments=redact_obj(parsed),
                        detail="validation_error",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - surface as a tool result, never crash the turn
                emit_custom_event(
                    "tool_authorization",
                    {
                        "tool": name,
                        "source": "agent_runtime",
                        "outcome": "execution_error",
                        "latencyMs": int((time.monotonic() - started) * 1000),
                    },
                )
                convo.append(
                    _tool_message(
                        call_id,
                        {"error": {"type": "execution_error", "message": redact(str(exc))}},
                    )
                )
                await record(
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
            emit_custom_event(
                "tool_authorization",
                {
                    "tool": name,
                    "source": "agent_runtime",
                    "outcome": "approved" if name in ctx.approvals else "ok",
                    "latencyMs": int((time.monotonic() - started) * 1000),
                },
            )
            await record(
                AgentStep(
                    kind="tool_result",
                    tool=name,
                    arguments=redact_obj(parsed),
                    result=redact_obj(raw_result),
                )
            )
            logger.info("agent tool ran: tool=%s", name)

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
    usage_agg = usage_agg.add(TokenUsage.parse(final.get("usage")))
    await record(AgentStep(kind="final", detail="max_iters"))
    return AgentRunResult(
        text=_final_text(final),
        model=deployment,
        steps=steps,
        iterations=iterations,
        usage=usage_agg,
    )
