"""Governed chat capability for invoking a saved workflow.

Only workflows whose resolved step agents use safe, workflow-compatible tools are
advertised. Nested execution receives a filtered capability builder and the normal
interactive approval policy, so the unattended runner's explicit approval bypass
cannot leak into a chat turn.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Any

from ..agents.agent_catalog import AgentCatalog
from ..agents.approvals import ApprovalPolicy
from ..agents.capabilities import CapabilityBuilder, Handler
from ..agents.runtime import (
    AgentRunResult, DelegatedAgentRunCancelled, DelegatedRunTrace, DelegatedToolResult,
)
from ..agents.synthetic_governance import synthetic_spec
from ..agents.tool_exec import CHAT_ONLY_SYNTHETIC_TOOL_NAMES, ToolContext, ToolExecutor
from ..agents.tools import ToolRegistry, ToolRisk
from ..catalog import DeploymentOption
from ..entitlements.service import EntitlementService
from ..gateway.client import ModelGatewayClient
from ..receipts import ReceiptRuntime, json_payload
from ..usage.service import UsageService
from .models import MAX_RUN_INPUT_LEN, RUN_WORKFLOW_TOOL_NAME, Workflow
from .runner import run_workflow
from .receipts import workflow_receipt
from .service import WorkflowService
from ..publishing.execution import (
    execution_scope, prepare_execution, workflow_publication_builder,
)
from ..publishing.models import PublicationError

MAX_WORKFLOW_CALLS_PER_TURN = 1
_RESULT_LIMIT = 6000


def workflow_tool_ineligible_reason(
    workflow: Workflow,
    *,
    composed: AgentCatalog,
    registry: ToolRegistry,
) -> str | None:
    """Return why a workflow cannot be a chat tool, or ``None`` when safe."""
    if not workflow.enabled:
        return "workflow is disabled"
    for index, step in enumerate(workflow.steps):
        agent = composed.get(step.agent)
        if agent is None or not agent.enabled:
            return f"step {index + 1} agent is unavailable"
        if agent.links:
            return f"step {index + 1} uses an orchestrator agent"
        effective = list(dict.fromkeys([*agent.tools, *step.extraTools]))
        for name in effective:
            if name == RUN_WORKFLOW_TOOL_NAME or name in CHAT_ONLY_SYNTHETIC_TOOL_NAMES:
                return f"step {index + 1} uses chat-only tool '{name}'"
            spec = registry.get(name) or synthetic_spec(name)
            if spec is None:
                return f"step {index + 1} uses unclassified tool '{name}'"
            if not spec.enabled:
                return f"step {index + 1} uses disabled tool '{name}'"
            if registry.get(name) is not None and not registry.is_allowlisted(name):
                return f"step {index + 1} uses blocked tool '{name}'"
            if spec.risk is not ToolRisk.safe or spec.needs_approval:
                return f"step {index + 1} uses non-safe tool '{name}'"
    return None


def safe_workflow_capability_builder(
    builder: CapabilityBuilder,
) -> CapabilityBuilder:
    """Filter shared workflow capabilities to classified safe reads only."""

    def _build(
        tool_names: Sequence[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Handler]]:
        tools, handlers = builder(tool_names)
        allowed: set[str] = set()
        safe_tools: list[dict[str, Any]] = []
        for schema in tools:
            function = schema.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(name, str):
                continue
            spec = synthetic_spec(name)
            if spec is None or spec.risk is not ToolRisk.safe or spec.needs_approval:
                continue
            allowed.add(name)
            safe_tools.append(schema)
        return safe_tools, {
            name: handler for name, handler in handlers.items() if name in allowed
        }

    return _build


async def eligible_workflows(
    service: WorkflowService,
    *,
    user_id: str,
    composed: AgentCatalog,
    registry: ToolRegistry,
) -> list[Workflow]:
    workflows = await service.available_for(user_id)
    return [
        workflow
        for workflow in workflows
        if workflow_tool_ineligible_reason(
            workflow, composed=composed, registry=registry
        )
        is None
    ]


def build_workflow_capability(
    *,
    workflows: list[Workflow],
    workflow_service: WorkflowService,
    composed: AgentCatalog,
    deployment: DeploymentOption,
    model_id: str,
    gateway: ModelGatewayClient,
    registry: ToolRegistry,
    executor: ToolExecutor,
    capabilities: CapabilityBuilder,
    entitlements: EntitlementService,
    metering: UsageService,
    user_id: str,
    session_id: str,
    api: str = "chat",
    state: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]]]:
    names = [workflow.name for workflow in workflows]
    descriptions = "; ".join(
        f"{workflow.name}: {workflow.description or workflow.displayName}"
        for workflow in workflows
    )
    budget = {"used": 0}
    schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": RUN_WORKFLOW_TOOL_NAME,
            "description": (
                "Run one of the user's saved, safe workflows over an input. "
                f"Available workflows: {descriptions}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workflow": {
                        "type": "string",
                        "enum": names,
                        "description": "The saved workflow to run.",
                    },
                    "input": {
                        "type": "string",
                        "maxLength": MAX_RUN_INPUT_LEN,
                        "description": "The input passed to the workflow's first step.",
                    },
                },
                "required": ["workflow", "input"],
                "additionalProperties": False,
            },
        },
    }
    safe_capabilities = safe_workflow_capability_builder(capabilities)

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if budget["used"] >= MAX_WORKFLOW_CALLS_PER_TURN:
            return {"error": "Only one saved workflow may run in a chat turn."}
        budget["used"] += 1
        name = str(args.get("workflow") or "").strip().lower()
        run_input = str(args.get("input") or "").strip()
        if name not in names:
            return {"error": "That workflow is not available to this chat agent."}
        if not run_input:
            return {"error": "Workflow input must not be empty."}
        if len(run_input) > MAX_RUN_INPUT_LEN:
            return {
                "error": f"Workflow input must be at most {MAX_RUN_INPUT_LEN} characters."
            }

        advertised = next(item for item in workflows if item.name == name)
        current = (
            await workflow_service.resolve_for(user_id, name)
            if advertised.sourceVersion is not None else await workflow_service.get(user_id, name)
        )
        if current is None:
            return {"error": "That workflow no longer exists."}
        if current.sourceVersion != advertised.sourceVersion:
            raise PublicationError("publication_version_changed")
        publication = None
        run_catalog = composed
        session = None
        if state is not None:
            session = await state.session_repo.get_session(user_id, session_id)
        if current.sourceVersion is not None:
            if state is None:
                raise PublicationError("publication_unavailable", 503)
            publication = await prepare_execution(
                state, current.sourceVersion, mode="workflow_tool", model_id=model_id,
                deployment=deployment, session=session,
            )
            run_catalog = await state.publications.dependency_catalog(
                publication.actor, publication.resolved.version,
            )
        reason = workflow_tool_ineligible_reason(
            current, composed=run_catalog, registry=registry
        )
        if reason is not None:
            return {"error": f"That workflow is no longer safe to run here: {reason}."}

        decision = await entitlements.check(user_id)
        if not decision.allowed:
            return {"error": decision.reason or "Workflow execution is not permitted."}

        async def prepare_tools(
            names: Sequence[str], nested: ToolContext
        ) -> tuple[ToolRegistry, ToolExecutor, ToolContext]:
            return registry, executor, replace(
                nested, prepare_model_context=ctx.prepare_model_context
            )

        with execution_scope(publication):
            outcome = await run_workflow(
                current, run_input=run_input, composed=run_catalog,
                deployment=deployment.deploymentName, gateway=gateway,
                registry=registry, executor=executor, capabilities=safe_capabilities,
                correlation_id=ctx.correlation_id, approval_policy=ApprovalPolicy.always,
                tool_builder=prepare_tools, api=api, model_id=model_id, pricing=metering.pricing,
                publication_builder=(
                    workflow_publication_builder(
                        state, model_id=model_id, deployment=deployment, session=session,
                    ) if state is not None else None
                ),
            )
        if outcome.usage.calls > 0:
            await metering.record_completion(
                user_id=user_id,
                session_id=session_id,
                model_id=model_id,
                deployment=deployment,
                usage=outcome.usage,
                status="complete" if outcome.ok else "error",
                provider_completed=True,
                agent=f"workflow:{current.name}",
                correlation_id=ctx.correlation_id,
            )
        nested_receipt = workflow_receipt(
            outcome,
            runtime=ReceiptRuntime(
                modelId=model_id, deployment=deployment.deploymentName,
                region=deployment.region, sku=deployment.sku, dataZone=deployment.dataZone,
                residency=deployment.residency, api=api, agent=f"workflow:{current.name}",
                workflowConfigSha256=json_payload(current.model_dump(mode="json")).sha256,
                publication=publication.evidence() if publication is not None else None,
            ),
            correlation_id=ctx.correlation_id, include_steps=True,
        )
        trace = DelegatedRunTrace(
            agent=f"workflow:{current.name}", usage=outcome.usage,
            iterations=sum(step.iterations for step in outcome.steps),
            status=nested_receipt.status, partial=nested_receipt.partial,
            receipt=nested_receipt, metered_separately=True,
        )
        if outcome.cancelled:
            raise DelegatedAgentRunCancelled(
                AgentRunResult(text=outcome.text, model=deployment.deploymentName, usage=outcome.usage),
                trace,
            )
        return DelegatedToolResult({
            "ok": outcome.ok,
            "workflow": current.name,
            # Workflows are immutable for the duration of this execution. The
            # durable updated timestamp is their current version marker and rides
            # into the parent turn's execution receipt with the raw tool result.
            "workflowVersion": current.updatedAt.isoformat(),
            "text": outcome.text[:_RESULT_LIMIT],
            "steps": [
                {"agent": step.agent, "ok": step.ok, "error": step.error}
                for step in outcome.steps
            ],
        }, trace=trace)

    return [schema], {RUN_WORKFLOW_TOOL_NAME: _handler}
