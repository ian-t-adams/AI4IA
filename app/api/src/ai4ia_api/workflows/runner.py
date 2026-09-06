"""Workflow **runner**: executes a saved pipeline of agent steps.

Steps run strictly in order on a **single** model deployment and provider API
(resolved once by the caller) so the whole run meters to one model. Each step is
an independent, depth-1 agent turn: a fresh two-message
conversation (the step agent's system prompt + the rendered instruction) with
**no** delegation capability injected — workflows are flat by construction, so a
step can never trigger another agent-as-tool sub-turn or recurse.

Steps DO get the execution-mode-independent synthetic capabilities (library, Web
IQ, memory) via the ``capabilities`` builder — see
:mod:`ai4ia_api.agents.capabilities`. Passing ``None`` means registry-only, which
is a two-tool surface (``calculator``, ``get_current_time``) regardless of what a
step's agent declares; that is a legitimate choice for a pure text transform but
must never be the accidental default, because the model then answers that it
cannot do the job and the run records that answer as a success.

Prompt rendering substitutes two placeholders **literally** (``str.replace``, not
``str.format`` — user text routinely contains stray ``{`` / ``}``):

* ``{input}``    — the original run input (constant across all steps).
* ``{previous}`` — the prior step's output (empty for step 1), truncated to
  :data:`MAX_CARRY_LEN` so a chatty step can't blow up the next step's context.

The runner is **total**: it never propagates a gateway/runtime exception. A failed
or unresolvable step stops the pipeline and yields ``ok=False`` with the
error text and the usage accumulated *so far*, so the endpoint can persist a
result and meter consumed tokens (a late failure can't be used to dodge billing).

Hard safety bounds for a run: at most :data:`~ai4ia_api.workflows.models.MAX_STEPS`
steps, each capped at :data:`_STEP_MAX_ITERS` model<->tool iterations, and each
step's tool fan-out bounded by ``run_agent_turn``'s own per-turn tool-call budget.
Worst case is roughly ``MAX_STEPS * (_STEP_MAX_ITERS + 1)`` model calls; entitlements
remain the cost backstop (the app ships unlimited-by-default per user).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ..agents.agent_catalog import AgentCatalog
from ..agents.approvals import ApprovalPolicy, ApprovalSink
from ..agents.activity import persisted_trace
from ..agents.capabilities import CapabilityBuilder, Handler
from ..agents.consent import ConsentChecker, ToolConsentSummary
from ..agents.receipt import ReceiptDraft
from ..agents.runtime import AgentRunCancelled, AgentRunFailed, AgentRunResult, run_agent_turn
from ..agents.tool_exec import ToolContext, ToolExecutor
from ..agents.tools import ToolRegistry
from ..gateway.client import ModelGatewayClient, ModelGatewayError
from ..receipts import ExecutionReceipt, ReceiptRuntime, json_payload
from ..safety import MessageSafety, attributed_safety, provider_for_api
from ..sessions.models import ActivityStep
from ..usage.models import TokenUsage
from .models import INPUT_TOKEN, PREVIOUS_TOKEN, Workflow, WorkflowStep

logger = logging.getLogger(__name__)

# Per-step model<->tool round-trip cap (kept tight; a workflow step is a focused
# transform, not an open-ended agent session).
_STEP_MAX_ITERS = 2
# Upper bound on how much of a step's output is carried into the next step's
# {previous} placeholder, so pipeline context growth is bounded regardless of how
# verbose any one step is.
MAX_CARRY_LEN = 8000
# Hard cap on a single step's *rendered* prompt. The per-field caps (instruction
# <= 4000, input <= 8000, previous truncated to MAX_CARRY_LEN) bound each input,
# but a step may legitimately reference {input} and {previous} once each
# (~4000 + 8000 + 8000). This cap blocks gross amplification — repeating a
# placeholder many times in one instruction to multiply a bounded input into a
# multi-MB request — which the per-field caps alone do not prevent.
MAX_RENDERED_PROMPT_LEN = 32000
ToolTurnBuilder = Callable[
    [Sequence[str], ToolContext],
    Awaitable[tuple[ToolRegistry, ToolExecutor, ToolContext]],
]


@dataclass
class WorkflowStepResult:
    """One executed (or attempted) step in a run trace."""

    agent: str
    ok: bool
    text: str = ""
    error: str | None = None
    iterations: int = 0
    receipt: ExecutionReceipt | None = None
    activity: list[ActivityStep] = field(default_factory=list)
    safety: MessageSafety | None = None
    cancelled: bool = False


@dataclass
class WorkflowRunResult:
    """Outcome of a whole workflow run.

    ``text`` is the final step's output on success, or the failure message on a
    controlled stop. ``usage`` is summed across every step that ran (so consumed
    tokens are always meterable, even on failure). ``ok`` is False if any step
    failed or could not be resolved.
    """

    ok: bool
    text: str
    usage: TokenUsage = field(default_factory=TokenUsage.empty)
    steps: list[WorkflowStepResult] = field(default_factory=list)
    cancelled: bool = False


def _render(instruction: str, *, run_input: str, previous: str) -> str:
    """Literal-substitute the two placeholders. ``str.replace`` (NOT ``str.format``)
    because both the instruction and the substituted values are user-controlled
    and may contain bare braces that would crash ``format``."""
    carried = previous[:MAX_CARRY_LEN]
    return instruction.replace(INPUT_TOKEN, run_input).replace(PREVIOUS_TOKEN, carried)


@dataclass
class StepOutcome:
    """Result of one step plus what the caller needs to keep sequencing.

    ``fatal`` means the pipeline must stop: the step could not be resolved, was
    rejected by a guard, or raised. ``usage`` is what *this* step consumed, so a
    caller can accumulate it even when ``fatal`` is set (a late failure must not
    be a way to dodge billing).
    """

    result: WorkflowStepResult
    usage: TokenUsage = field(default_factory=TokenUsage.empty)
    fatal: bool = False


async def run_workflow_step(
    step: WorkflowStep,
    *,
    index: int,
    workflow_name: str,
    run_input: str,
    previous: str,
    composed: AgentCatalog,
    deployment: str,
    gateway: ModelGatewayClient,
    registry: ToolRegistry,
    executor: ToolExecutor,
    capabilities: CapabilityBuilder | None = None,
    correlation_id: str | None = None,
    approval_policy: ApprovalPolicy = ApprovalPolicy.always,
    consent_checker: ConsentChecker | None = None,
    tool_consent: ToolConsentSummary | None = None,
    tool_builder: ToolTurnBuilder | None = None,
    api: str = "chat",
) -> StepOutcome:
    """Execute a single workflow step. Total: never raises.

    Extracted so the in-request runner and the durable orchestration's activity
    execute byte-identical logic. The guards below are load-bearing (orchestrator
    rejection, prompt-amplification cap), and duplicating them per execution mode
    is exactly how two paths drift into different security postures.

    ``capabilities`` injects the same synthetic, user-bound tools chat offers
    (library, Web IQ, memory). Without it a step runs with **only** the two
    registry built-ins no matter what its agent declares, and the model answers
    that it cannot do the job — a wrong answer persisted as a successful run.

    The tool list handed to both the capability builder and the turn is the
    agent's own plus ``step.extraTools``. The memory tools in particular are
    *attach-gated* — offered only when named — so a step targeting a curated
    agent (whose tool list is fixed and not user-editable) cannot save a memory
    unless the step adds the tool itself.

    ``index`` is 0-based; user-facing messages report ``index + 1``.
    """
    def rejected(error: str, *, cancelled: bool = False) -> StepOutcome:
        return StepOutcome(
            result=WorkflowStepResult(
                agent=step.agent, ok=False, error=error, cancelled=cancelled,
                receipt=ExecutionReceipt(
                    runtime=ReceiptRuntime(
                        deployment=deployment, api=api, agent=step.agent,
                    ),
                    toolConsent=tool_consent,
                    status="cancelled" if cancelled else "error",
                    partial=True, notes=["workflow_step_not_started"],
                ),
            ),
            fatal=True,
        )

    target = composed.get(step.agent)
    if target is None or not target.enabled:
        err = f"Step {index + 1}: agent '{step.agent}' is unavailable."
        return rejected(err)
    if consent_checker is not None:
        try:
            check = await consent_checker("", "")
        except asyncio.CancelledError:
            return rejected(f"Step {index + 1}: workflow cancelled.", cancelled=True)
        except Exception:
            logger.warning("workflow step approval could not be checked")
            return rejected(
                f"Step {index + 1}: tool approval could not be checked. Retry the run."
            )
        if check.reason not in {None, "consent_not_granted"}:
            if check.reason == "entitlement_denied":
                return rejected(
                    f"Step {index + 1}: an account or usage limit blocked the run. "
                    "Retry later or ask an administrator to review your limits."
                )
            err = (
                f"Step {index + 1}: tool approval was revoked, expired, disabled or changed. "
                "Review the enabled tools and start a new run with explicit approval."
            )
            return rejected(err, cancelled=check.reason == "consent_revoked")

    # Reject orchestrator agents as steps: their delegation capability is only
    # wired in the chat path, so running one here would silently lose it. This
    # is a runtime check (links are dynamic, not known at write time).
    if target.links:
        err = (
            f"Step {index + 1}: agent '{step.agent}' is an orchestrator (it links "
            "to other agents) and can't be used as a workflow step. Use a "
            "leaf agent here, or run the orchestrator via @mention in chat."
        )
        return rejected(err)

    prompt = _render(step.instruction, run_input=run_input, previous=previous)
    # Block placeholder amplification: a step that repeats {input}/{previous}
    # could expand a bounded input into an unbounded request. Stop gracefully
    # with the usage consumed so far rather than firing an oversized call.
    if len(prompt) > MAX_RENDERED_PROMPT_LEN:
        err = (
            f"Step {index + 1}: rendered prompt exceeds {MAX_RENDERED_PROMPT_LEN} "
            "characters. Reduce the instruction or avoid repeating the "
            "{input}/{previous} placeholders."
        )
        return rejected(err)

    messages = [
        {"role": "system", "content": target.systemPrompt},
        {"role": "user", "content": prompt},
    ]
    # A step runs with its agent's declared tools PLUS whatever it adds. Order is
    # preserved and duplicates dropped so a tool the agent already declares is not
    # offered to the model twice.
    effective_tools = list(target.tools)
    for extra in step.extraTools:
        if extra not in effective_tools:
            effective_tools.append(extra)
    extra_tools: list[dict[str, Any]] = []
    extra_handlers: dict[str, Handler] = {}
    if capabilities is not None:
        try:
            extra_tools, extra_handlers = capabilities(effective_tools)
        except Exception:  # noqa: BLE001 — a capability must never break a step.
            logger.warning(
                "workflow '%s' step %d (agent=%s): capability build failed",
                workflow_name,
                index + 1,
                step.agent,
                exc_info=True,
            )
            return rejected(
                f"Step {index + 1}: enabled tools could not be prepared. Retry the run."
            )
    sink = ApprovalSink()
    ctx = ToolContext(
        correlation_id=correlation_id, approval_policy=approval_policy,
        # Previous step output and agent-authored input are untrusted to a new
        # step. A clean original user input can retain injection-only ergonomics.
        untrusted_context=bool(previous),
        approval_sink=sink, consent_checker=consent_checker,
    )
    if tool_builder is not None:
        try:
            registry, executor, ctx = await tool_builder(effective_tools, ctx)
        except asyncio.CancelledError:
            return rejected(f"Step {index + 1}: workflow cancelled.", cancelled=True)
        except Exception:
            logger.warning("workflow step tool contracts could not be prepared")
            return rejected(f"Step {index + 1}: tool contracts are unavailable. Retry the run.")

    def finished(
        run: AgentRunResult, *, error: str | None = None,
        state: Literal["complete", "incomplete", "error", "cancelled"] = "complete",
    ) -> WorkflowStepResult:
        safety = attributed_safety(run.safety, provider_for_api(api))
        draft = ReceiptDraft(
            correlation_id=correlation_id,
            runtime=ReceiptRuntime(
                deployment=deployment, api=api, agent=target.name,
                instructionSource="agent",
                agentConfigSha256=json_payload(target.model_dump(mode="json")).sha256,
            ),
            prompt_messages=messages,
            tool_consent=tool_consent,
        )
        return WorkflowStepResult(
            agent=step.agent, ok=error is None, text=run.text, error=error,
            iterations=run.iterations, activity=persisted_trace(run.steps),
            safety=safety, cancelled=state == "cancelled",
            receipt=draft.build(
                steps=run.steps, iterations=run.iterations, status=state,
                partial=error is not None, approvals_requested=len(sink),
                offered=run.offered_tools, prompt_messages=run.effective_prompt or messages,
                model_requests=run.model_requests, usage=run.usage, safety=safety,
                delegations=run.delegations,
            ),
        )

    try:
        run = await run_agent_turn(
            deployment=deployment,
            messages=messages,
            tool_names=effective_tools,
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ctx,
            params=None,
            max_iters=_STEP_MAX_ITERS,
            extra_tools=extra_tools,
            extra_handlers=extra_handlers,
            api=api,
            retain_failed_request=True,
        )
    except AgentRunCancelled as exc:
        return StepOutcome(
            result=finished(
                exc.partial, error=f"Step {index + 1}: workflow cancelled.", state="cancelled",
            ),
            usage=exc.partial.usage, fatal=True,
        )
    except asyncio.CancelledError:
        return rejected(f"Step {index + 1}: workflow cancelled.", cancelled=True)
    except ModelGatewayError as exc:
        logger.warning(
            "workflow '%s' step %d (agent=%s) gateway failed status=%d",
            workflow_name,
            index + 1,
            step.agent,
            exc.status_code,
        )
        err = f"Step {index + 1}: agent '{step.agent}' failed while running."
        outcome = rejected(err)
        outcome.usage = TokenUsage.parse(None)
        return outcome
    except AgentRunFailed as exc:
        if isinstance(exc.cause, ModelGatewayError):
            logger.warning(
                "workflow '%s' step %d (agent=%s) gateway failed status=%d",
                workflow_name, index + 1, step.agent, exc.cause.status_code,
            )
        else:
            logger.warning(
                "workflow '%s' step %d (agent=%s) failed after partial execution",
                workflow_name, index + 1, step.agent,
            )
        err = f"Step {index + 1}: agent '{step.agent}' failed while running."
        return StepOutcome(
            result=finished(exc.partial, error=err, state="error"),
            usage=exc.partial.usage,
            fatal=True,
        )
    except Exception:  # noqa: BLE001 — total runner: never propagate.
        logger.warning(
            "workflow '%s' step %d (agent=%s) failed",
            workflow_name,
            index + 1,
            step.agent,
        )
        err = f"Step {index + 1}: agent '{step.agent}' failed while running."
        return rejected(err)

    if run.incomplete:
        err = f"Step {index + 1}: agent '{step.agent}' returned an incomplete response."
        return StepOutcome(
            result=finished(run, error=err, state="incomplete"),
            usage=run.usage,
            fatal=True,
        )

    blocked = next(
        (item for item in run.steps if item.kind in {"tool_denied", "tool_error"}), None
    )
    if blocked is not None:
        err = (
            f"Step {index + 1}: a tool call was blocked ({blocked.detail or 'denied'}). "
            "Review the enabled tools and run again with explicit tool approval; "
            "unattended runs cannot wait for an approval prompt."
        )
        return StepOutcome(
            result=finished(
                run, error=err,
                state="cancelled" if blocked.detail == "consent_revoked" else "error",
            ),
            usage=run.usage, fatal=True,
        )
    return StepOutcome(result=finished(run), usage=run.usage)


async def run_workflow(
    workflow: Workflow,
    *,
    run_input: str,
    composed: AgentCatalog,
    deployment: str,
    gateway: ModelGatewayClient,
    registry: ToolRegistry,
    executor: ToolExecutor,
    capabilities: CapabilityBuilder | None = None,
    correlation_id: str | None = None,
    approval_policy: ApprovalPolicy = ApprovalPolicy.always,
    consent_checker: ConsentChecker | None = None,
    tool_consent: ToolConsentSummary | None = None,
    tool_builder: ToolTurnBuilder | None = None,
    api: str = "chat",
) -> WorkflowRunResult:
    """Run ``workflow`` end-to-end and return a total, never-raising result.

    ``composed`` is the caller's per-user catalog (curated + user agents) used to
    resolve each step's agent at run time. ``deployment`` is the single Azure
    deployment all steps execute on (one model, one bill). ``capabilities`` is
    forwarded per step — see :func:`run_workflow_step`.
    """
    usage = TokenUsage.empty()
    trace: list[WorkflowStepResult] = []
    previous = ""

    for i, step in enumerate(workflow.steps):
        outcome = await run_workflow_step(
            step,
            index=i,
            workflow_name=workflow.name,
            run_input=run_input,
            previous=previous,
            composed=composed,
            deployment=deployment,
            gateway=gateway,
            registry=registry,
            executor=executor,
            capabilities=capabilities,
            correlation_id=correlation_id,
            approval_policy=approval_policy,
            consent_checker=consent_checker,
            tool_consent=tool_consent,
            tool_builder=tool_builder,
            api=api,
        )
        usage = usage.add(outcome.usage)
        trace.append(outcome.result)
        if outcome.fatal:
            return WorkflowRunResult(
                ok=False,
                text=outcome.result.error or "Workflow step failed.",
                usage=usage,
                steps=trace,
                cancelled=outcome.result.cancelled,
            )
        previous = outcome.result.text

    return WorkflowRunResult(ok=True, text=previous, usage=usage, steps=trace)
