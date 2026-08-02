"""Workflow **runner**: executes a saved pipeline of agent steps.

Steps run strictly in order on a **single** model deployment (resolved once by the
caller, already past the Responses-API guard) so the whole run meters to one
model. Each step is an independent, depth-1 agent turn: a fresh two-message
conversation (the step agent's system prompt + the rendered instruction) with
**no** delegation capability injected — workflows are flat by construction, so a
step can never trigger another agent-as-tool sub-turn or recurse.

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

from ..agents.agent_catalog import AgentCatalog
from ..agents.runtime import run_agent_turn
from ..agents.tool_exec import ToolContext, ToolExecutor
from ..agents.tools import ToolRegistry
from ..gateway.client import ModelGatewayClient
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
    correlation_id: str | None = None,
) -> StepOutcome:
    """Execute a single workflow step. Total: never raises.

    Extracted so the in-request runner and the durable orchestration's activity
    execute byte-identical logic. The guards below are load-bearing (orchestrator
    rejection, prompt-amplification cap), and duplicating them per execution mode
    is exactly how two paths drift into different security postures.

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
    try:
        run = await run_agent_turn(
            deployment=deployment,
            messages=messages,
            tool_names=target.tools,
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ToolContext(correlation_id=correlation_id),
            params=None,
            max_iters=_STEP_MAX_ITERS,
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
    correlation_id: str | None = None,
) -> WorkflowRunResult:
    """Run ``workflow`` end-to-end and return a total, never-raising result.

    ``composed`` is the caller's per-user catalog (curated + user agents) used to
    resolve each step's agent at run time. ``deployment`` is the single Azure
    deployment all steps execute on (one model, one bill).
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
