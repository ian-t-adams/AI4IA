"""Saved-workflow chat tool: eligibility, safe filtering, and execution re-checks."""
from __future__ import annotations

from typing import Any, cast

from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec
from ai4ia_api.agents.tool_exec import ToolContext, build_tools
from ai4ia_api.catalog import DeploymentOption
from ai4ia_api.entitlements.models import EntitlementDecision
from ai4ia_api.usage.memory_repo import InMemoryUsageRepository
from ai4ia_api.usage.pricing import PricingBook
from ai4ia_api.usage.service import UsageService
from ai4ia_api.workflows.capability import (
    RUN_WORKFLOW_TOOL_NAME,
    build_workflow_capability,
    safe_workflow_capability_builder,
    workflow_tool_ineligible_reason,
)
from ai4ia_api.workflows.models import Workflow, WorkflowStep
from ai4ia_api.workflows.service import WorkflowService


def _agent(*, tools: list[str]) -> AgentSpec:
    return AgentSpec(
        name="worker",
        displayName="Worker",
        description="",
        systemPrompt="Do the work.",
        tools=tools,
    )


def _workflow() -> Workflow:
    return Workflow(
        id="safe-flow",
        userId="u1",
        name="safe-flow",
        displayName="Safe flow",
        description="Transform the input safely.",
        steps=[
            WorkflowStep(
                agent="worker",
                instruction="Transform {input}",
            )
        ],
    )


def test_only_safe_workflow_compatible_tools_are_eligible():
    registry, _executor = build_tools()
    workflow = _workflow()

    safe = workflow_tool_ineligible_reason(
        workflow,
        composed=AgentCatalog(agents=[_agent(tools=["calculator"])]),
        registry=registry,
    )
    external = workflow_tool_ineligible_reason(
        workflow,
        composed=AgentCatalog(agents=[_agent(tools=["web_search"])]),
        registry=registry,
    )
    recursive = workflow_tool_ineligible_reason(
        workflow,
        composed=AgentCatalog(agents=[_agent(tools=[RUN_WORKFLOW_TOOL_NAME])]),
        registry=registry,
    )

    assert safe is None
    assert external == "step 1 uses non-safe tool 'web_search'"
    assert recursive == "step 1 uses chat-only tool 'run_workflow'"


def test_nested_capability_builder_filters_external_and_destructive_tools():
    async def handler(_args: dict[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        return {}

    def unfiltered(_names):
        schemas = [
            {
                "type": "function",
                "function": {"name": name, "parameters": {"type": "object"}},
            }
            for name in ("recall_memory", "web_search", "remember_memory")
        ]
        return schemas, {
            name: handler
            for name in ("recall_memory", "web_search", "remember_memory")
        }

    schemas, handlers = safe_workflow_capability_builder(unfiltered)([])
    names = [schema["function"]["name"] for schema in schemas]

    assert names == ["recall_memory"]
    assert set(handlers) == {"recall_memory"}


class _WorkflowService:
    def __init__(self, current: Workflow) -> None:
        self.current = current

    async def get(self, _user_id: str, _name: str) -> Workflow | None:
        return self.current


class _Entitlements:
    async def check(self, _user_id: str) -> EntitlementDecision:
        return EntitlementDecision(allowed=True)


class _Gateway:
    def __init__(self) -> None:
        self.calls = 0
        self.offered: list[str] = []

    async def complete(
        self, *, deployment, messages, params=None, correlation_id=None, api="chat"
    ):
        self.calls += 1
        self.offered = [
            tool["function"]["name"] for tool in (params or {}).get("tools", [])
        ]
        return {
            "choices": [{"message": {"content": "workflow done"}}],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 2,
                "total_tokens": 4,
            },
        }


async def test_handler_runs_safe_workflow_and_meters_nested_model_usage():
    workflow = _workflow()
    agent = _agent(tools=["calculator"])
    registry, executor = build_tools()
    gateway = _Gateway()
    usage_repo = InMemoryUsageRepository()
    metering = UsageService(
        usage_repo, PricingBook({}, currency="USD", version=None)
    )

    tools, handlers = build_workflow_capability(
        workflows=[workflow],
        workflow_service=cast(WorkflowService, _WorkflowService(workflow)),
        composed=AgentCatalog(agents=[agent]),
        deployment=DeploymentOption(
            region="eastus2",
            dataZone="us",
            sku="GlobalStandard",
            deploymentName="dep",
        ),
        model_id="gpt-x",
        gateway=cast(Any, gateway),
        registry=registry,
        executor=executor,
        capabilities=lambda _names: ([], {}),
        entitlements=cast(Any, _Entitlements()),
        metering=metering,
        user_id="u1",
        session_id="s1",
    )

    assert tools[0]["function"]["name"] == RUN_WORKFLOW_TOOL_NAME
    result = await handlers[RUN_WORKFLOW_TOOL_NAME](
        {"workflow": workflow.name, "input": "hello"},
        ToolContext(correlation_id="cid"),
    )

    assert result["ok"] is True
    assert result["text"] == "workflow done"
    assert gateway.calls == 1
    stored = usage_repo._by_user["u1"]
    assert len(stored) == 1
    assert stored[0].agent == "workflow:safe-flow"
    assert stored[0].totalTokens == 4


async def test_zero_model_call_failure_does_not_pollute_usage_ledger():
    workflow = _workflow().model_copy(
        update={
            "steps": [
                WorkflowStep(
                    agent="worker",
                    instruction="{input}{input}{input}{input}{input}",
                )
            ]
        }
    )
    registry, executor = build_tools()
    gateway = _Gateway()
    usage_repo = InMemoryUsageRepository()
    _tools, handlers = build_workflow_capability(
        workflows=[workflow],
        workflow_service=cast(WorkflowService, _WorkflowService(workflow)),
        composed=AgentCatalog(agents=[_agent(tools=[])]),
        deployment=DeploymentOption(
            region="eastus2",
            dataZone="us",
            sku="GlobalStandard",
            deploymentName="dep",
        ),
        model_id="gpt-x",
        gateway=cast(Any, gateway),
        registry=registry,
        executor=executor,
        capabilities=lambda _names: ([], {}),
        entitlements=cast(Any, _Entitlements()),
        metering=UsageService(
            usage_repo, PricingBook({}, currency="USD", version=None)
        ),
        user_id="u1",
        session_id="s1",
    )

    result = await handlers[RUN_WORKFLOW_TOOL_NAME](
        {"workflow": workflow.name, "input": "x" * 8000}, ToolContext()
    )

    assert result["ok"] is False
    assert "rendered prompt exceeds" in result["text"]
    assert gateway.calls == 0
    assert usage_repo._by_user.get("u1", []) == []


async def test_handler_rechecks_safety_after_workflow_changes():
    original = _workflow()
    changed = original.model_copy(
        update={
            "steps": [
                WorkflowStep(
                    agent="worker",
                    instruction="Search {input}",
                    extraTools=["web_search"],
                )
            ]
        }
    )
    registry, executor = build_tools()
    gateway = _Gateway()
    tools, handlers = build_workflow_capability(
        workflows=[original],
        workflow_service=cast(WorkflowService, _WorkflowService(changed)),
        composed=AgentCatalog(agents=[_agent(tools=[])]),
        deployment=DeploymentOption(
            region="eastus2",
            dataZone="us",
            sku="GlobalStandard",
            deploymentName="dep",
        ),
        model_id="gpt-x",
        gateway=cast(Any, gateway),
        registry=registry,
        executor=executor,
        capabilities=lambda _names: ([], {}),
        entitlements=cast(Any, _Entitlements()),
        metering=UsageService(
            InMemoryUsageRepository(),
            PricingBook({}, currency="USD", version=None),
        ),
        user_id="u1",
        session_id="s1",
    )

    result = await handlers[RUN_WORKFLOW_TOOL_NAME](
        {"workflow": original.name, "input": "hello"}, ToolContext()
    )

    assert tools
    assert result["error"].startswith("That workflow is no longer safe")
    assert gateway.calls == 0
