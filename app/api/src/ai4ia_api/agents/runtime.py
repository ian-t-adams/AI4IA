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
registry (defense in depth on top of schema-time filtering), and every *external
or destructive* invocation is additionally gated on a fresh, per-call human
approval bound to the exact arguments the model emitted (see
``agents/approvals.py``). The second gate is deliberately independent of the
first: the registry's approval check keys off a tool's standing posture, which a
``trusted`` MCP server switches off, and standing trust is precisely what an
indirect prompt injection borrows when it chooses an outbound call's arguments.
Argument/result content is only ever credential-redacted (``redact_obj``) before
it lands on the in-process ``AgentStep`` trace, and free-text log lines never
include it at all — only the tool name and fixed, bounded status/reason strings
reach logs or the
user-facing activity view (see ``agents.activity``). The tool "name" itself is
model-supplied and unvalidated at call time, so it is never trusted as log-safe
free text either: only a name that is BOTH known (a synthetic capability or a
registered registry tool) AND matches a bounded, canonical charset
(``tools.is_safe_tool_name``) is ever surfaced in a step/log/telemetry event.
Registry/handler membership alone is not sufficient proof of safety — a tool
name can originate from a source this codebase does not author or code-review
(e.g. a name a remote MCP server advertises), so a name that is well-formed
enough to dispatch but still adversarial (newlines, control characters) must be
sentineled just the same as a wholly hallucinated one. Any disqualified value is
replaced with the fixed sentinel ``"unknown_tool"`` before it can reach any
persisted or logged surface. Dispatch decisions themselves (authorization,
execution, denial-looping) still use the raw name so real tool calls — however
they are named — are routed correctly.
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
from .approvals import (
    arguments_digest,
    approval_key,
    draft_for_call,
    requires_invocation_approval,
)
from .tool_exec import ToolContext, ToolExecutor, ToolValidationError
from .tools import DenyReason, ToolRegistry, is_safe_tool_name, redact, redact_obj

logger = logging.getLogger(__name__)

# Per-turn safety bounds. ``max_iters`` caps model<->tool round trips; the call
# budget caps total tool invocations across the whole turn (one iteration may ask
# for several tools); result truncation caps how much a single tool can inject
# back into the context window.
_DEFAULT_MAX_ITERS = 4
_MAX_TOOL_CALLS = 8
_MAX_TOOL_RESULT_BYTES = 8192

# What the model is told when a call is held for human approval. It is phrased so
# the turn ends in a useful sentence ("I need you to approve X") instead of the
# model retrying the same call until the budget is spent — and it deliberately
# carries no hint about how to get around the gate.
_APPROVAL_HELD_MESSAGE = (
    "This call was not executed. It requires the user's explicit approval for "
    "these exact arguments, and the user has been shown an approval prompt. Do "
    "not retry it in this turn and do not attempt a different tool to achieve "
    "the same effect. Tell the user plainly what you need approved and why."
)

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
    resolved_tool_names = [ctx.tool_aliases.get(name, name) for name in tool_names]
    real_schema = executor.schema_for(resolved_tool_names, registry=registry, ctx=ctx)
    # Runtime dispatch alias -> durable governance name, for the human-readable
    # label on an approval prompt. Built here because ``tool_aliases`` maps the
    # other way and the runtime only ever sees the alias.
    labels: dict[str, str] = {alias: name for name, alias in ctx.tool_aliases.items()}
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
    # Turn-local taint. Starts from the caller's assessment of the context blocks
    # it injected (documents / recalled memory / library excerpts) and latches ON
    # as soon as ANY tool result comes back, because a tool result is untrusted
    # remote content that the model has now read. That is the exact chain the
    # audit describes: a hostile MCP/web response steering a later outbound call.
    untrusted_context = ctx.untrusted_context
    # Mutable copy of the redeemed per-invocation approvals. A key is REMOVED
    # the moment it authorizes a dispatch, so one approval buys exactly one
    # execution.
    #
    # This is not a refinement — it is the difference between "one click, one
    # call" and "one click, up to _MAX_TOOL_CALLS calls". ``PendingToolApproval
    # .consumed`` makes *redemption* single-use, but redemption happens once per
    # turn in the router while this set is consulted once per tool call, and the
    # model's tool-call list is precisely what injected context influences. A
    # membership test that never shrinks therefore lets the attacker choose the
    # repeat count, which for a ``destructive`` tool is that many unauthorized
    # side effects from a single human decision. Repeats fall through to the
    # normal held-for-approval path, so the user is asked again rather than the
    # model quietly retrying.
    unspent_approvals: set[str] = set(ctx.invocation_approvals)

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
            # `name` is model-supplied and unvalidated at this point -- it must
            # never reach logs, telemetry, or the persisted/live activity trace
            # verbatim (an attacker-influenced completion could otherwise smuggle
            # arbitrary free text into those surfaces under the guise of a "tool
            # name"). `safe_name` is the only value used for anything surfaced;
            # dispatch below keeps using the raw `name` so real authorization/
            # execution routes correctly. Registered/handler membership alone is
            # NOT sufficient: a dynamically-supplied name (e.g. MCP-discovered)
            # could be genuinely dispatchable yet still contain newlines or other
            # content crafted to forge a different log line, so the name must
            # also match a bounded, canonical charset before it is trusted.
            known = name in handlers or registry.get(name) is not None
            safe_name = name if known and is_safe_tool_name(name) else "unknown_tool"

            tool_calls_used += 1
            if tool_calls_used > _MAX_TOOL_CALLS:
                convo.append(
                    _tool_message(
                        call_id,
                        {"error": {"type": "budget_exceeded", "message": "tool budget exhausted"}},
                    )
                )
                await record(AgentStep(kind="tool_error", tool=safe_name, detail="budget_exceeded"))
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
                await record(AgentStep(kind="tool_error", tool=safe_name, detail="invalid_arguments"))
                continue

            # Emit a pre-execution marker so the UI can show "running X" live. It
            # is NOT persisted (only the finalized result/denied/error step is).
            await record(
                AgentStep(kind="tool_start", tool=safe_name, arguments=redact_obj(parsed)),
                persist=False,
            )

            # Synthetic capabilities (e.g. delegate_to_agent) are dispatched here,
            # before the registry path. They are disjoint from real tool names
            # (asserted at setup), count against the shared tool-call budget, and
            # are wrapped so a handler error becomes a structured tool result.
            #
            # SEAM (deliberate gap, stated plainly): synthetic capabilities carry
            # no ``ToolSpec``, so the per-invocation approval gate below cannot
            # read a risk off them and does NOT cover them. ``browse_url`` in
            # particular is an egress channel that stays ungated. Closing that
            # means giving each synthetic capability a governed spec (or an
            # explicit gated-name set on ``ToolContext``) and running it through
            # the same digest check; see agents/approvals.py.
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
                            tool=safe_name,
                            arguments=redact_obj(parsed),
                            detail="execution_error",
                        )
                    )
                    logger.warning("agent delegate error: tool=%s", safe_name)
                    continue
                untrusted_context = True
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
                        tool=safe_name,
                        arguments=redact_obj(parsed),
                        result=redact_obj(raw_result),
                    )
                )
                logger.info("agent delegated: tool=%s", safe_name)
                continue

            # --- Per-invocation approval (agents/approvals.py) ---------------
            # Deliberately layered ON TOP of registry authorization rather than
            # folded into it: the registry's approval check keys off the tool's
            # *standing* posture, and standing posture is exactly what a
            # ``trusted`` MCP server switches off. Being standing-approved says
            # "this agent may use this tool"; it must never say "this agent may
            # send THESE arguments to that host". The digest below is computed
            # from the arguments the model actually emitted, so an approval the
            # user granted for one call cannot authorize a different one.
            #
            # Ordering is deliberate too. The registry runs FIRST so cheap,
            # structural denials keep their own precise reason (a disabled or
            # unallowlisted tool must report ``disabled``/``not_allowlisted``,
            # not a spurious approval prompt for a call that could never run).
            # Only a call that would otherwise execute — or one the registry
            # itself held for approval — reaches a human.
            spec = registry.get(name)
            needs_invocation_approval = spec is not None and requires_invocation_approval(
                spec,
                policy=ctx.approval_policy,
                untrusted_context=untrusted_context,
            )
            digest = arguments_digest(parsed) if needs_invocation_approval else ""
            approval_token = approval_key(name, digest) if needs_invocation_approval else ""
            invocation_approved = (
                needs_invocation_approval and approval_token in unspent_approvals
            )
            decision = registry.authorize(
                name,
                granted_scopes=ctx.granted_scopes,
                target_hosts=ctx.target_hosts,
                approved=(name in ctx.approvals) or invocation_approved,
            )
            held_for_approval = (
                decision.allowed and needs_invocation_approval and not invocation_approved
            ) or (
                not decision.allowed
                and decision.reason is DenyReason.approval_required
                and needs_invocation_approval
            )
            # ``spec is not None`` is implied by needs_invocation_approval; it is
            # restated so the narrowing is local and obvious rather than inferred
            # three statements up.
            if held_for_approval and spec is not None:
                reason = DenyReason.approval_required.value
                if ctx.approval_sink is not None:
                    ctx.approval_sink.request(
                        draft_for_call(
                            spec,
                            tool=name,
                            label=labels.get(name),
                            arguments=parsed,
                            digest=digest,
                        )
                    )
                emit_custom_event(
                    "tool_authorization",
                    {
                        "tool": safe_name,
                        "source": "agent_runtime",
                        "outcome": "denied",
                        "reason": reason,
                        "gate": "invocation_approval",
                    },
                )
                emit_security_block("tool_authorization", reason, "agent_runtime")
                convo.append(
                    _tool_message(
                        call_id,
                        {"error": {"type": reason, "message": _APPROVAL_HELD_MESSAGE}},
                    )
                )
                await record(AgentStep(kind="tool_denied", tool=safe_name, detail=reason))
                logger.info("agent tool held for approval: tool=%s", safe_name)
                # One held call per tool is informative; a second means the model
                # is looping, so cut tools off and force a final answer.
                if name in denied_once:
                    force_final = True
                denied_once.add(name)
                continue
            if not decision.allowed:
                reason = decision.reason.value if decision.reason else "denied"
                emit_custom_event(
                    "tool_authorization",
                    {
                        "tool": safe_name,
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
                await record(AgentStep(kind="tool_denied", tool=safe_name, detail=reason))
                logger.info("agent tool denied: tool=%s reason=%s", safe_name, reason)
                # A second denial of the same tool means the model is looping;
                # cut tools off so the next call must produce a final answer.
                if name in denied_once:
                    force_final = True
                denied_once.add(name)
                continue

            # Spend the approval BEFORE dispatching, not after a successful
            # return. The handler is what makes the outbound call, so once it is
            # entered the side effect may already have happened even if it then
            # raises; treating the approval as "authorizes one attempt" means a
            # failed call cannot be silently retried against the same click. The
            # cost is that a genuinely failed call needs re-approval, which is
            # the correct direction to err for an egress control.
            if invocation_approved:
                unspent_approvals.discard(approval_token)

            started = time.monotonic()
            try:
                raw_result = await executor.execute(name, parsed, ctx)
            except ToolValidationError as exc:
                emit_custom_event(
                    "tool_authorization",
                    {
                        "tool": safe_name,
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
                        tool=safe_name,
                        arguments=redact_obj(parsed),
                        detail="validation_error",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - surface as a tool result, never crash the turn
                emit_custom_event(
                    "tool_authorization",
                    {
                        "tool": safe_name,
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
                        tool=safe_name,
                        arguments=redact_obj(parsed),
                        detail="execution_error",
                    )
                )
                logger.warning("agent tool error: tool=%s", safe_name)
                continue

            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _truncate(json.dumps(raw_result, default=str)),
                }
            )
            # A tool result is remote content the model has now read: from here on
            # this turn is tainted, so a later external call is gated even under
            # the ``tainted`` policy.
            untrusted_context = True
            emit_custom_event(
                "tool_authorization",
                {
                    "tool": safe_name,
                    "source": "agent_runtime",
                    "outcome": (
                        "approved"
                        if invocation_approved or name in ctx.approvals
                        else "ok"
                    ),
                    "latencyMs": int((time.monotonic() - started) * 1000),
                },
            )
            await record(
                AgentStep(
                    kind="tool_result",
                    tool=safe_name,
                    arguments=redact_obj(parsed),
                    result=redact_obj(raw_result),
                )
            )
            logger.info("agent tool ran: tool=%s", safe_name)

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
