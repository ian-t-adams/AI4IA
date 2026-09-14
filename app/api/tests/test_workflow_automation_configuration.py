from types import SimpleNamespace

import pytest

from ai4ia_api.workflows.automation_factory import check_workflow_automation_ready
from ai4ia_api.workflows.cosmos_store import CosmosWorkflowStore
from tests.conftest import make_settings


@pytest.mark.parametrize("change", [
    {"durable_workflows_enabled": False},
    {"session_deletion_enabled": False},
    {"usage_metering_enabled": False},
    {"durable_workflow_timeout_seconds": 0},
])
def test_enabled_automation_requires_real_parent_prerequisites(change):
    base = dict(
        workflow_approvals_enabled=True, workflow_scheduling_enabled=True,
        durable_workflows_enabled=True, session_deletion_enabled=True,
        usage_metering_enabled=True, durable_workflow_timeout_seconds=1800,
    )
    make_settings(**base).validate_runtime()
    with pytest.raises(RuntimeError):
        make_settings(**{**base, **change}).validate_runtime()


def test_scheduling_does_not_enable_approvals_implicitly():
    make_settings().validate_runtime()
    with pytest.raises(RuntimeError, match="requires resumable"):
        make_settings(workflow_scheduling_enabled=True, workflow_approvals_enabled=False).validate_runtime()


class Metadata:
    def __init__(self):
        self.properties = {"partitionKey": {"paths": ["/userId"]}}
        self.reads = 0

    async def read(self):
        self.reads += 1
        return self.properties


@pytest.mark.parametrize("invalid", [
    {"partitionKey": {"paths": ["/sessionId"]}},
    {"defaultTtl": 3600},
    {"analyticalStorageTtl": -1},
])
async def test_coordination_layout_and_retention_are_verified_before_enable(invalid):
    workflow, usage = Metadata(), Metadata()
    existing = object.__new__(CosmosWorkflowStore)
    existing._container = workflow
    state = SimpleNamespace(
        settings=SimpleNamespace(workflow_approvals_enabled=False, workflow_scheduling_enabled=False, env="prod"),
        workflow_service=SimpleNamespace(_store=existing),
        usage=SimpleNamespace(_repo=SimpleNamespace(_usage=usage)),
    )
    await check_workflow_automation_ready(state)
    assert workflow.reads == usage.reads == 0
    state.settings.workflow_approvals_enabled = True
    await check_workflow_automation_ready(state)
    assert workflow.reads == usage.reads == 1
    workflow.properties = {**workflow.properties, **invalid}
    with pytest.raises(RuntimeError, match="owner partitions"):
        await check_workflow_automation_ready(state)


async def test_enabled_schedules_need_system_rules_but_disabled_features_do_not(monkeypatch, tmp_path):
    from ai4ia_api.workflows.automation_common import AutomationError

    state = SimpleNamespace(settings=SimpleNamespace(
        workflow_approvals_enabled=False, workflow_scheduling_enabled=False, env="local",
    ))
    monkeypatch.setenv("PYTHONTZPATH", str(tmp_path))
    await check_workflow_automation_ready(state)
    state.settings.workflow_scheduling_enabled = True
    with pytest.raises(AutomationError, match="unavailable"):
        await check_workflow_automation_ready(state)
