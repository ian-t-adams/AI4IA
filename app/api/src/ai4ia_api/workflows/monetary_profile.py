"""Explicit source selection for the first, limited monetary workflow profile."""
from __future__ import annotations

from ..agents.agent_catalog import AgentCatalog
from ..request_constraints import automatic_memory_allowed, tools_allowed
from .automation_common import AutomationError, ExecutionLimits
from .models import Workflow


def require_capped_profile(
    workflow: Workflow, agents: AgentCatalog, documents: list[str], limits: ExecutionLimits,
) -> None:
    if limits.maxSpendMicroUsd is None:
        return
    if tools_allowed() or automatic_memory_allowed():
        raise AutomationError(
            "spend_profile_unsupported",
            "The capped text-only profile requires explicit tools=false and automatic memory=false.",
            status=422,
        )
    if documents or any(
        step.extraTools or (agent := agents.get(step.agent)) is None or agent.tools or agent.links
        for step in workflow.steps
    ):
        raise AutomationError(
            "spend_profile_unsupported",
            "The selected workflow requires tools or resources outside the capped text-only profile.",
            status=422,
        )
