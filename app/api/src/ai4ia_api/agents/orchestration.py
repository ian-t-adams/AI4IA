"""Multi-agent orchestration: the ``delegate_to_agent`` capability.

An agent may declare ``links`` — names of other agents it is allowed to hand a
self-contained sub-task to. This module turns those links into a single synthetic
``delegate_to_agent`` tool (an OpenAI function schema + an async handler) that is
injected into the supervisor's :func:`~ai4ia_api.agents.runtime.run_agent_turn`
loop. The model decides when to delegate; the handler resolves the target from the
caller's *composed* catalog, runs it as a sub-turn, and returns its answer.

Design invariants (kept deliberately strict for safety and correct metering):

* **Depth-1 only.** Sub-agents are run with **no** extra capabilities, so a linked
  agent's own links are inert during delegation — there is no recursion and cycles
  are structurally impossible regardless of how users wire their links.
* **One deployment, one bill.** Sub-agents run on the **supervisor's** deployment,
  so every model call in the turn meters to a single model/deployment and there is
  no sub-model resolution/availability surface.
* **Bounded fan-out.** At most :data:`MAX_DELEGATIONS_PER_TURN` delegations per
  turn, each sub-turn capped at :data:`_SUB_AGENT_MAX_ITERS` iterations.
* **Runtime resolution.** Links are not existence-checked at write time; an
  unknown/disabled/non-linked target returns a structured ``{"error": ...}`` tool
  result the supervisor can react to, never an exception.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..gateway.client import ModelGatewayClient, ModelGatewayError
from ..usage.models import TokenUsage
from .agent_catalog import AgentCatalog, AgentSpec
from .runtime import (
    AgentRunFailed,
    AgentRunResult,
    DelegatedAgentRunFailed,
    DelegatedRunTrace,
    DelegatedToolResult,
    run_agent_turn,
)
from .tool_exec import ToolContext, ToolExecutor
from .tools import ToolRegistry
from .user_agents import MAX_LINKS, NAME_RE

logger = logging.getLogger(__name__)

DELEGATE_TOOL_NAME = "delegate_to_agent"
# Hard cap on how many delegations a single supervisor turn may perform. Each
# sub-turn is bounded to _SUB_AGENT_MAX_ITERS iterations plus (worst case) one
# final no-tools call, so this bounds total model calls to roughly
# (supervisor.max_iters + 1) + MAX_DELEGATIONS_PER_TURN * (_SUB_AGENT_MAX_ITERS + 1).
MAX_DELEGATIONS_PER_TURN = 2
_SUB_AGENT_MAX_ITERS = 2
_MAX_TASK_LEN = 8000

DelegateHandler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


def sanitize_links(orchestrator_name: str, links: list[str] | None) -> list[str]:
    """Normalize an agent's links for runtime use: lowercase, valid ``@mention``
    grammar, de-duplicated, never the agent itself, and capped at
    :data:`~ai4ia_api.agents.user_agents.MAX_LINKS`. Mirrors the write-time
    validation in :class:`~ai4ia_api.agents.service.AgentService` so that curated
    agents (which skip that path) and any older records are still safe — a
    malformed or oversized ``agents.json`` entry can never inflate the synthetic
    tool schema."""
    self_name = (orchestrator_name or "").strip().lower()
    seen: set[str] = set()
    out: list[str] = []
    for raw in links or []:
        name = (raw or "").strip().lower()
        if not name or name == self_name or name in seen or not NAME_RE.match(name):
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= MAX_LINKS:
            break
    return out


def build_delegate_capability(
    *,
    orchestrator: AgentSpec,
    composed: AgentCatalog,
    gateway: ModelGatewayClient,
    registry: ToolRegistry,
    executor: ToolExecutor,
    deployment: str,
) -> tuple[list[dict[str, Any]], dict[str, DelegateHandler], list[TokenUsage]]:
    """Build the ``delegate_to_agent`` synthetic tool for an orchestrator.

    Returns ``(extra_tools, extra_handlers, usage_sink)`` ready to pass to
    :func:`run_agent_turn`. ``usage_sink`` accumulates the :class:`TokenUsage` of
    every sub-turn (on the supervisor's deployment) so the caller can meter the
    full turn as ``run.usage + sum(usage_sink)``. If the orchestrator has no
    valid links, all three are empty (no tool advertised).
    """
    links = sanitize_links(orchestrator.name, orchestrator.links)
    usage_sink: list[TokenUsage] = []
    if not links:
        return [], {}, usage_sink

    # Per-turn delegation counter (closure state shared with the handler).
    budget = {"used": 0}

    schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": DELEGATE_TOOL_NAME,
            "description": (
                "Delegate a self-contained sub-task to one of your linked specialist "
                "agents and receive its answer. The target agent sees ONLY the 'task' "
                "string — it has no access to this conversation, the user's memory, or "
                "any uploaded documents — so include every piece of context it needs. "
                "Use the returned answer to compose your own final reply to the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "enum": links,
                        "description": "Which linked agent to delegate to.",
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "The complete, self-contained task or question for the "
                            "linked agent (no outside context is shared)."
                        ),
                    },
                },
                "required": ["agent", "task"],
                "additionalProperties": False,
            },
        },
    }

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        target_name = str(args.get("agent") or "").strip().lower()
        task = args.get("task")
        if target_name not in links:
            return {"error": f"'{target_name}' is not one of your linked agents."}
        if not isinstance(task, str) or not task.strip():
            return {"error": "task must be a non-empty string."}
        if len(task) > _MAX_TASK_LEN:
            return {"error": f"task must be at most {_MAX_TASK_LEN} characters."}
        if budget["used"] >= MAX_DELEGATIONS_PER_TURN:
            return {"error": "delegation budget exhausted for this turn."}
        target = composed.get(target_name)
        if target is None or not target.enabled:
            return {"error": f"agent '{target_name}' is unavailable."}

        budget["used"] += 1
        sub_messages = [
            {"role": "system", "content": target.systemPrompt},
            {"role": "user", "content": task},
        ]
        sub_context = ToolContext(correlation_id=ctx.correlation_id)
        sub_offered_tools = executor.schema_for(
            target.tools,
            registry=registry,
            ctx=sub_context,
        )
        # Depth-1: no extra_tools/extra_handlers, so the sub-agent cannot itself
        # delegate. Supervisor deployment + params=None for correct, simple
        # metering and to avoid inheriting the parent's sampling/token budget.
        try:
            run = await run_agent_turn(
                deployment=deployment,
                messages=sub_messages,
                tool_names=target.tools,
                gateway=gateway,
                registry=registry,
                executor=executor,
                ctx=sub_context,
                params=None,
                max_iters=_SUB_AGENT_MAX_ITERS,
            )
        except AgentRunFailed as exc:
            raise DelegatedAgentRunFailed(
                cause=exc.cause,
                partial=exc.partial,
                trace=DelegatedRunTrace(
                    agent=target_name,
                    effective_prompt=exc.partial.effective_prompt or sub_messages,
                    model_requests=exc.partial.model_requests,
                    offered_tools=exc.partial.offered_tools,
                    steps=exc.partial.steps,
                    iterations=exc.partial.iterations,
                    usage=exc.partial.usage,
                    safety=exc.partial.safety,
                    status="error",
                    partial=True,
                ),
            ) from exc
        except ModelGatewayError as exc:
            partial = AgentRunResult(
                text="",
                model=deployment,
                iterations=1,
                usage=TokenUsage.parse(None),
                effective_prompt=sub_messages,
                model_requests=[sub_messages],
                offered_tools=sub_offered_tools,
            )
            raise DelegatedAgentRunFailed(
                cause=exc,
                partial=partial,
                trace=DelegatedRunTrace(
                    agent=target_name,
                    effective_prompt=sub_messages,
                    iterations=1,
                    usage=partial.usage,
                    status="error",
                    partial=True,
                ),
            ) from exc
        usage_sink.append(run.usage)
        logger.info(
            "delegated to agent=%s iters=%s", target_name, run.iterations
        )
        return DelegatedToolResult(
            {"agent": target_name, "answer": run.text},
            trace=DelegatedRunTrace(
                agent=target_name,
                effective_prompt=run.effective_prompt,
                model_requests=run.model_requests,
                offered_tools=run.offered_tools,
                steps=run.steps,
                iterations=run.iterations,
                usage=run.usage,
                safety=run.safety,
            ),
        )

    return [schema], {DELEGATE_TOOL_NAME: _handler}, usage_sink
