"""Workflow **runner**: executes a saved pipeline of agent steps.

Steps run strictly in order on a **single** model deployment (resolved once by the
caller, already past the Responses-API guard) so the whole run meters to one
model. Each step is an independent, depth-1 agent turn: a fresh two-message
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

import logging
from dataclasses import dataclass, field
from typing import Any

from ..agents.agent_catalog import AgentCatalog
from ..agents.approvals import ApprovalPolicy
from ..agents.capabilities import CapabilityBuilder, Handler
from ..agents.runtime import AgentRunFailed, run_agent_turn
from ..agents.tool_exec import ToolContext, ToolExecutor
from ..agents.tools import ToolRegistry
from ..gateway.client import ModelGatewayClient, ModelGatewayError
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


@dataclass
class WorkflowStepResult:
    """One executed (or attempted) step in a run trace."""

    agent: str
    ok: bool
    text: str = ""
    error: str | None = None
    iterations: int = 0


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
    target = composed.get(step.agent)
    if target is None or not target.enabled:
        err = f"Step {index + 1}: agent '{step.agent}' is unavailable."
        return StepOutcome(
            result=WorkflowStepResult(agent=step.agent, ok=False, error=err),
            fatal=True,
        )

    # Reject orchestrator agents as steps: their delegation capability is only
    # wired in the chat path, so running one here would silently lose it. This
    # is a runtime check (links are dynamic, not known at write time).
    if target.links:
        err = (
            f"Step {index + 1}: agent '{step.agent}' is an orchestrator (it links "
            "to other agents) and can't be used as a workflow step. Use a "
            "leaf agent here, or run the orchestrator via @mention in chat."
        )
        return StepOutcome(
            result=WorkflowStepResult(agent=step.agent, ok=False, error=err),
            fatal=True,
        )

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
        return StepOutcome(
            result=WorkflowStepResult(agent=step.agent, ok=False, error=err),
            fatal=True,
        )

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
            # Degrade to registry-only rather than failing the run: the step may
            # not need the synthetic tools at all. Logged so a systematically
            # broken builder is visible instead of showing up as a model that
            # mysteriously says it cannot do its job.
            logger.warning(
                "workflow '%s' step %d (agent=%s): capability build failed",
                workflow_name,
                index + 1,
                step.agent,
                exc_info=True,
            )
    try:
        run = await run_agent_turn(
            deployment=deployment,
            messages=messages,
            tool_names=effective_tools,
            gateway=gateway,
            registry=registry,
            executor=executor,
            # EXPLICIT exemption from per-invocation approval, recorded rather
            # than inherited. A workflow run is unattended by construction: there
            # is no open request to return a grant on and no one watching to
            # click it, so "hold for approval" here does not mean "ask" — it
            # means "deny, silently, forever". Leaving the default ``always`` in
            # place would have broken every step that reads a document and then
            # writes memory, or searches the web, the moment those capabilities
            # acquired specs (see agents/synthetic_governance.py) — turning a
            # security fix into a feature outage with no signal.
            #
            # This is therefore the one place the P1-13 seam stays open, and it
            # is stated plainly rather than hidden behind an absent attribute.
            # What still applies: the step's tool surface is built from the
            # agent's own declared tools by a builder the owner configured, every
            # capability is closure-bound to that user, and the registry path is
            # unaffected. Closing it properly needs a durable, out-of-band
            # approval channel for unattended runs — a product feature, not a
            # flag flip, and a decision for the owner rather than a default to
            # change quietly here.
            ctx=ToolContext(
                correlation_id=correlation_id,
                approval_policy=ApprovalPolicy.off,
            ),
            params=None,
            max_iters=_STEP_MAX_ITERS,
            extra_tools=extra_tools,
            extra_handlers=extra_handlers,
        )
    except ModelGatewayError as exc:
        logger.warning(
            "workflow '%s' step %d (agent=%s) gateway failed status=%d",
            workflow_name,
            index + 1,
            step.agent,
            exc.status_code,
        )
        err = f"Step {index + 1}: agent '{step.agent}' failed while running."
        return StepOutcome(
            result=WorkflowStepResult(agent=step.agent, ok=False, error=err),
            usage=TokenUsage.parse(None),
            fatal=True,
        )
    except AgentRunFailed as exc:
        logger.warning(
            "workflow '%s' step %d (agent=%s) failed after partial execution",
            workflow_name,
            index + 1,
            step.agent,
        )
        err = f"Step {index + 1}: agent '{step.agent}' failed while running."
        return StepOutcome(
            result=WorkflowStepResult(
                agent=step.agent,
                ok=False,
                text=exc.partial.text,
                error=err,
                iterations=exc.partial.iterations,
            ),
            usage=exc.partial.usage,
            fatal=True,
        )
    except Exception as exc:  # noqa: BLE001 — total runner: never propagate.
        logger.warning(
            "workflow '%s' step %d (agent=%s) failed: %s",
            workflow_name,
            index + 1,
            step.agent,
            exc,
        )
        err = f"Step {index + 1}: agent '{step.agent}' failed while running."
        return StepOutcome(
            result=WorkflowStepResult(agent=step.agent, ok=False, error=err),
            fatal=True,
        )

    return StepOutcome(
        result=WorkflowStepResult(
            agent=step.agent, ok=True, text=run.text, iterations=run.iterations
        ),
        usage=run.usage,
    )


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
        )
        usage = usage.add(outcome.usage)
        trace.append(outcome.result)
        if outcome.fatal:
            return WorkflowRunResult(
                ok=False,
                text=outcome.result.error or "Workflow step failed.",
                usage=usage,
                steps=trace,
            )
        previous = outcome.result.text

    return WorkflowRunResult(ok=True, text=previous, usage=usage, steps=trace)
