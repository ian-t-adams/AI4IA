"""Borrow existing workflow/session/usage infrastructure; never provision it."""
from __future__ import annotations

from typing import Any

from .automation_access import WorkflowAccess
from .automation_service import WorkflowAutomationService
from .automation_store import CosmosAutomationStore, InMemoryAutomationStore
from .cosmos_store import CosmosWorkflowStore
from .store import InMemoryWorkflowStore


async def check_workflow_automation_ready(state: Any) -> None:
    if state.settings.workflow_scheduling_enabled:
        from .scheduling import require_timezone_data

        require_timezone_data()
    if not state.settings.workflow_approvals_enabled or state.settings.env == "local":
        return
    existing = state.workflow_service._store
    usage = state.usage._repo
    if not isinstance(existing, CosmosWorkflowStore) or not hasattr(usage, "_usage"):
        raise RuntimeError("Workflow automation requires canonical Cosmos coordination and usage.")
    for container in (existing._container, usage._usage):
        properties = await container.read()
        if (
            properties.get("partitionKey", {}).get("paths") != ["/userId"]
            or properties.get("defaultTtl") not in (None, -1)
            or properties.get("analyticalStorageTtl") not in (None, 0)
        ):
            raise RuntimeError("Workflow coordination requires owner partitions and nonexpiring canonical storage.")


def build_workflow_automation(state: Any) -> WorkflowAutomationService:
    existing = state.workflow_service._store
    if isinstance(existing, CosmosWorkflowStore):
        store = CosmosAutomationStore(existing._container)
    elif isinstance(existing, InMemoryWorkflowStore):
        store = InMemoryAutomationStore()
    else:
        raise RuntimeError("Workflow automation requires a supported canonical workflow store.")
    return WorkflowAutomationService(
        state, store, WorkflowAccess(state, getattr(state, "publications", None)),
    )
