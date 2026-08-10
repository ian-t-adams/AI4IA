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
``agents/approvals.py``). That second gate covers **both** dispatch routes: a
registry tool reads its risk from its registered ``ToolSpec``, and a *synthetic*
capability injected through ``extra_handlers`` reads its risk from
``agents/synthetic_governance.py``. A synthetic capability with no classification
there is refused rather than run, so a new capability cannot acquire an execution
route without also acquiring a risk. The second gate is deliberately independent
of the first: the registry's approval check keys off a tool's standing posture,
which a ``trusted`` MCP server switches off, and standing trust is precisely what
an indirect prompt injection borrows when it chooses an outbound call's arguments.
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

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..gateway.client import ModelGatewayClient
from ..usage.models import TokenUsage
from ..logging_setup import emit_custom_event, emit_security_block
from .prompt_budget import (
    TOOL_CONTEXT_RESERVE_TOKENS,
    bound_agent_context,
    prompt_byte_budget,
    serialized_budget_bytes,
)
from .approvals import (
    arguments_digest,
    approval_key,
    draft_for_call,
    requires_invocation_approval,
)
from .streaming import stream_iteration
from .synthetic_governance import synthetic_spec
from .tool_exec import ToolContext, ToolExecutor, ToolValidationError
from .tools import DenyReason, ToolRegistry, ToolSpec, is_safe_tool_name, redact, redact_obj

logger = logging.getLogger(__name__)

# Per-turn safety bounds. ``max_iters`` caps model<->tool round trips; the call
# budget caps total tool invocations across the whole turn (one iteration may ask
# for several tools); result truncation caps how much a single tool can inject
# back into the context window.
_DEFAULT_MAX_ITERS = 4
_MAX_TOOL_CALLS = 8
_MAX_TOOL_RESULT_BYTES = 8192
_DEFAULT_MAX_OUTPUT_TOKENS = 1024

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
    # Everything handed to ``on_delta`` this turn, concatenated in order — empty
    # unless the caller asked for token streaming. It is deliberately NOT the
    # same as ``text``: ``text`` is the FINAL iteration's answer, while this also
    # contains any preamble the model emitted alongside a tool call ("Let me look
    # that up..."), which the user has already seen. A streaming caller must
    # persist this, so the saved row matches what was actually delivered.
    streamed_text: str = ""


class AgentRunFailed(RuntimeError):
    """A failed model round trip with the safe work completed so far."""

    def __init__(self, *, cause: Exception, partial: AgentRunResult) -> None:
        super().__init__("agent model round trip failed")
        self.cause: Exception = cause
        self.partial: AgentRunResult = partial


class AgentContextBudgetError(RuntimeError):
    """The fixed/current tool-loop context cannot fit without breaking protocol."""


def _tool_message(call_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": _truncate(json.dumps(payload)),
    }


def _truncate(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= _MAX_TOOL_RESULT_BYTES:
        return text
    return encoded[:_MAX_TOOL_RESULT_BYTES].decode("utf-8", "ignore") + "...[truncated]"


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
    on_delta: Callable[[str], Awaitable[None]] | None = None,
    prompt_budget_bytes: int | None = None,
) -> AgentRunResult:
    """Run a single agent turn with tool calling and return the final answer.

    ``extra_tools``/``extra_handlers`` inject *synthetic* capabilities (e.g. the
    ``delegate_to_agent`` orchestration tool) on top of the registry-backed tools.
    Each synthetic tool is an OpenAI function schema in ``extra_tools`` plus an
    async handler in ``extra_handlers`` keyed by the function name. Synthetic
    names MUST be disjoint from the real executor tool names (asserted below, fail
    closed) so a synthetic capability can never shadow a governed tool. Synthetic
    handlers bypass the registry authorize/execute path but are **not**
    ungoverned: each one's risk is declared in ``agents/synthetic_governance.py``
    and runs through the same per-invocation approval gate as a registry tool, an
    unclassified one is refused, and they are still counted against the per-turn
    tool-call budget and wrapped so an exception becomes a structured tool result.

    ``on_delta`` opts the turn into **token streaming** (audit finding P1-16).
    With it set, each model iteration is consumed over SSE and every assistant
    text increment is forwarded the moment it arrives, so a tool-using turn shows
    text and live tool activity while it works instead of going silent for a full
    round trip. Without it the turn takes the original non-streaming
    ``gateway.complete`` path byte-for-byte — which is also the automatic
    behaviour when the injected gateway exposes no ``stream``. Nothing else about
    the turn changes: the same schema is advertised, the same governance runs on
    the same reassembled tool calls, and the same bounds apply.
    """
    convo: list[dict[str, Any]] = [dict(m) for m in messages]
    current_user_index = next(
        (
            index
            for index in range(len(convo) - 1, -1, -1)
            if convo[index].get("role") == "user"
        ),
        -1,
    )
    if current_user_index < 0:
        raise AgentContextBudgetError("Agent input has no current user message.")
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
    effective_prompt_budget = prompt_budget_bytes or (
        prompt_byte_budget(
            None, dict(params or {}), default_max_tokens=_DEFAULT_MAX_OUTPUT_TOKENS
        )
        + TOOL_CONTEXT_RESERVE_TOKENS
    )
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
    # Token streaming is opt-in AND capability-checked: a caller that wants
    # deltas still gets the original non-streaming path against an injected
    # gateway that has no ``stream`` (workflow runners, delegation sub-turns and
    # the whole existing test surface), rather than an AttributeError.
    stream_tokens = on_delta is not None and callable(getattr(gateway, "stream", None))
    streamed_parts: list[str] = []
    completed_text_parts: list[str] = []
    completed_model_calls = 0
    current_stream_usage: dict[str, Any] | None = None

    async def emit_delta(text: str) -> None:
        """Forward one assistant text increment, best-effort.

        Swallowing here matches ``record`` below: a UI/stream consumer must never
        be able to break a turn. The router tracks what it actually yielded, so a
        dropped callback cannot make the persisted row claim undelivered text.
        """
        if not text or on_delta is None:
            return
        streamed_parts.append(text)
        try:
            await on_delta(text)
        except Exception:  # noqa: BLE001 - a UI callback must never break a turn
            logger.debug("on_delta callback failed", exc_info=True)

    def observe_stream_usage(usage: dict[str, Any]) -> None:
        nonlocal current_stream_usage
        current_stream_usage = usage

    async def call_model(request_params: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        """One model round trip, streamed or not, folded into shared usage.

        Returns ``(assistant text, tool calls)``. The two transports are
        deliberately reduced to the same pair here so everything downstream —
        governance, budget, trace, taint — has exactly one shape to reason about.
        """
        nonlocal completed_model_calls, current_stream_usage, current_user_index
        nonlocal usage_agg, convo
        current_stream_usage = None
        offered_schema = request_params.get("tools") or []
        schema_bytes = (
            serialized_budget_bytes({"tools": offered_schema, "tool_choice": "auto"})
            if offered_schema
            else 0
        )
        try:
            convo, dropped_messages, dropped_exchanges = bound_agent_context(
                convo,
                current_user_index=current_user_index,
                prompt_budget_bytes=effective_prompt_budget,
                additional_fixed_bytes=schema_bytes,
            )
            current_user_index = next(
                index
                for index in range(len(convo) - 1, -1, -1)
                if convo[index].get("role") == "user"
            )
            if dropped_messages or dropped_exchanges:
                emit_custom_event(
                    "agent_context_truncated",
                    {
                        "stage": "model_iteration",
                        "droppedMessages": dropped_messages,
                        "droppedExchanges": dropped_exchanges,
                    },
                )
        except ValueError as exc:
            raise AgentContextBudgetError(
                "Fixed agent input and offered tool schemas exceed the model context budget."
            ) from exc
        try:
            if stream_tokens:
                iteration = await stream_iteration(
                    gateway=gateway,
                    deployment=deployment,
                    messages=convo,
                    params=request_params,
                    correlation_id=ctx.correlation_id,
                    on_delta=emit_delta,
                    on_usage=observe_stream_usage,
                )
                completed_model_calls += 1
                usage_agg = usage_agg.add(TokenUsage.parse(iteration.usage))
                return iteration.content, iteration.tool_calls
            result = await gateway.complete(
                deployment=deployment,
                messages=convo,
                params=request_params,
                correlation_id=ctx.correlation_id,
            )
            completed_model_calls += 1
            usage_agg = usage_agg.add(TokenUsage.parse(result.get("usage")))
            message = (result.get("choices") or [{}])[0].get("message") or {}
            content = message.get("content") or ""
            completed_text_parts.append(content)
            return content, message.get("tool_calls") or []
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not (
                streamed_parts
                or current_stream_usage is not None
                or completed_model_calls
                or usage_agg.calls
                or steps
            ):
                raise
            partial_text = (
                "".join(streamed_parts) if stream_tokens else "".join(completed_text_parts)
            )
            partial = AgentRunResult(
                text=partial_text,
                model=deployment,
                steps=list(steps),
                iterations=iterations,
                usage=usage_agg.add(TokenUsage.parse(current_stream_usage)),
                streamed_text="".join(streamed_parts),
            )
            raise AgentRunFailed(cause=exc, partial=partial) from exc

    def finish(text: str, iterations: int) -> AgentRunResult:
        return AgentRunResult(
            text=text,
            model=deployment,
            steps=steps,
            iterations=iterations,
            usage=usage_agg,
            streamed_text="".join(streamed_parts),
        )

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

    # --- Denial paths, shared by both dispatch routes -------------------------
    # Factored out because the synthetic and registry routes must produce
    # byte-identical governance output: the same telemetry event, the same
    # security block, the same structured tool result, the same trace step. Two
    # copies of this would be two things to keep in step, and the one that
    # drifted would be the one nobody was watching.
    #
    # Both return True when the caller should stop offering tools: a second
    # denial of the same tool means the model is looping on it.

    async def deny(
        *, name: str, safe_name: str, call_id: str | None, reason: str
    ) -> bool:
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
            _tool_message(call_id, {"error": {"type": "authorization_denied", "message": reason}})
        )
        await record(AgentStep(kind="tool_denied", tool=safe_name, detail=reason))
        logger.info("agent tool denied: tool=%s reason=%s", safe_name, reason)
        repeat = name in denied_once
        denied_once.add(name)
        return repeat

    async def hold_for_approval(
        *,
        spec: ToolSpec,
        name: str,
        safe_name: str,
        call_id: str | None,
        parsed: dict[str, Any],
        digest: str,
    ) -> bool:
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
            _tool_message(call_id, {"error": {"type": reason, "message": _APPROVAL_HELD_MESSAGE}})
        )
        await record(AgentStep(kind="tool_denied", tool=safe_name, detail=reason))
        logger.info("agent tool held for approval: tool=%s", safe_name)
        repeat = name in denied_once
        denied_once.add(name)
        return repeat

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
        content, tool_calls = await call_model(req_params)

        if not tool_calls:
            await record(AgentStep(kind="final"))
            return finish(content, iterations)

        # Preserve the assistant tool-call message verbatim (content may be null)
        # so the subsequent tool results reference valid call ids.
        convo.append(
            {
                "role": "assistant",
                "content": content or None,
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
            # The governing spec is resolved from ONE of two sources — the
            # registry for a real tool, ``synthetic_governance`` for a synthetic
            # capability — and everything after that is shared. That is the
            # point: a capability cannot acquire a dispatch route without also
            # acquiring a risk classification, which is precisely what
            # ``extra_handlers`` used to let it do (audit finding P1-13). If a
            # synthetic capability has no classification it is REFUSED, not run.
            is_synthetic = name in handlers
            spec = synthetic_spec(name) if is_synthetic else registry.get(name)
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

            # Synthetic capabilities (web search, browse_url, memory,
            # delegate_to_agent, ...) are dispatched here, before the registry
            # path. They are disjoint from real tool names (asserted at setup),
            # count against the shared tool-call budget, and are wrapped so a
            # handler error becomes a structured tool result.
            #
            # Ordering mirrors the registry path below: the structural check —
            # "is this capability governed at all?" — runs FIRST, so an
            # unclassified capability reports its own precise reason instead of
            # minting an approval prompt for a call that must not run either way.
            if is_synthetic:
                if spec is None:
                    logger.warning(
                        "ungoverned synthetic capability refused: tool=%s", safe_name
                    )
                    if await deny(
                        name=name,
                        safe_name=safe_name,
                        call_id=call_id,
                        reason=DenyReason.ungoverned.value,
                    ):
                        force_final = True
                    continue
                if needs_invocation_approval and not invocation_approved:
                    if await hold_for_approval(
                        spec=spec,
                        name=name,
                        safe_name=safe_name,
                        call_id=call_id,
                        parsed=parsed,
                        digest=digest,
                    ):
                        force_final = True
                    continue
                # Spend before dispatch, for the reason spelled out on the
                # registry path below: entering the handler may already have
                # caused the side effect.
                if invocation_approved:
                    unspent_approvals.discard(approval_token)
                try:
                    raw_result = await handlers[name](parsed, ctx)
                except AgentRunFailed as exc:
                    for nested_step in exc.partial.steps:
                        await record(nested_step)
                    await record(
                        AgentStep(
                            kind="tool_error",
                            tool=safe_name,
                            detail="delegate_failed",
                        )
                    )
                    partial_text = (
                        "".join(streamed_parts)
                        if stream_tokens
                        else "".join(completed_text_parts)
                    )
                    partial = AgentRunResult(
                        text=partial_text,
                        model=deployment,
                        steps=list(steps),
                        iterations=iterations,
                        usage=usage_agg.add(exc.partial.usage),
                        streamed_text="".join(streamed_parts),
                    )
                    raise AgentRunFailed(cause=exc.cause, partial=partial) from exc.cause
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

            # Ordering is deliberate here too. The registry runs FIRST so cheap,
            # structural denials keep their own precise reason (a disabled or
            # unallowlisted tool must report ``disabled``/``not_allowlisted``,
            # not a spurious approval prompt for a call that could never run).
            # Only a call that would otherwise execute — or one the registry
            # itself held for approval — reaches a human.
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
                if await hold_for_approval(
                    spec=spec,
                    name=name,
                    safe_name=safe_name,
                    call_id=call_id,
                    parsed=parsed,
                    digest=digest,
                ):
                    force_final = True
                continue
            if not decision.allowed:
                reason = decision.reason.value if decision.reason else "denied"
                if await deny(
                    name=name, safe_name=safe_name, call_id=call_id, reason=reason
                ):
                    force_final = True
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
    # must respond in natural language rather than request yet another tool. This
    # streams too when the turn is streaming — otherwise the last thing the user
    # waits on would be the one round trip that still went silent.
    tail_text, _ = await call_model(dict(base_params))
    await record(AgentStep(kind="final", detail="max_iters"))
    return finish(tail_text, iterations)
