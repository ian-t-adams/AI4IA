"""Retired video selections refuse workflow admission, never silently narrow."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial
import json

import httpx
import pytest

from ai4ia_api.agents.user_agents import UserAgent
from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.workflows.automation_access import WorkflowSelection
from ai4ia_api.workflows.automation_common import AutomationError, ExecutionLimits
from ai4ia_api.workflows.durable import build_orchestration_payload
from ai4ia_api.workflows.models import Workflow, WorkflowStep, WorkflowValidationError
from ai4ia_api.workflows.runner import run_workflow, run_workflow_step
from tests.test_durable_workflows import _StubDurable
from tests.test_agent_runtime import _assistant_text, _assistant_tool_call
from tests.test_workflow_automation_service import install

TOOL = "generate_video"


def install_retirement(client):
    automation, calls, sent = install(client)

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        offered = {item["function"]["name"] for item in body.get("tools", [])}
        result = (
            _assistant_tool_call("calculate", "calculator", '{"expression":"6*7"}')
            if len(calls) == 1 and "calculator" in offered else _assistant_text("42")
        )
        return httpx.Response(200, json=result)

    client.app.state.gateway = ModelGatewayClient(
        client.app.state.settings, httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    return automation, calls, sent


def seed(client, selection, retired):
    session = client.post("/api/sessions", json={
        "model": "gpt-5.4", "libraryDocumentIds": [],
    }).json()
    agent = UserAgent(
        id="legacy-creator", name="legacy-creator", displayName="Legacy creator",
        userId=session["userId"], systemPrompt="Calculate carefully.",
        tools=["calculator", *([TOOL] if retired and selection == "agent" else [])],
    )
    workflow = Workflow(
        id="legacy-flow", name="legacy-flow", displayName="Legacy flow",
        userId=session["userId"], steps=[
            WorkflowStep(agent="testleaf", instruction="Calculate {input}."),
            WorkflowStep(
                agent=agent.name, instruction="Check {previous}.",
                extraTools=[TOOL] if retired and selection == "step" else [],
            ),
        ],
    )
    client.portal.call(client.app.state.agent_service._store.put, agent)
    client.portal.call(client.app.state.workflow_service._store.put, workflow)
    return session, agent, workflow


def assert_unchanged(client, agent, workflow):
    assert client.portal.call(
        client.app.state.agent_service._store.get, agent.userId, agent.name,
    ) == agent
    assert client.portal.call(
        client.app.state.workflow_service._store.get, workflow.userId, workflow.name,
    ) == workflow


def test_ordinary_chat_refuses_active_legacy_agent_without_rewriting_it(client):
    _, calls, _ = install_retirement(client)
    session, agent, workflow = seed(client, "agent", True)
    response = client.post("/api/chat", json={
        "sessionId": session["id"], "content": "@legacy-creator calculate 6*7", "stream": False,
    })
    assert response.status_code == 422, response.text
    assert "generate_video is retired" in response.text
    assert calls == []
    assert client.get(f"/api/sessions/{session['id']}/messages").json() == []
    assert_unchanged(client, agent, workflow)


@pytest.mark.parametrize("selection", ["agent", "step"])
@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("durable", [False, True])
def test_manual_workflow_refuses_retired_selection_before_inference_or_enqueue(
    client, selection, retired, durable,
):
    _, calls, _ = install_retirement(client)
    scheduler = _StubDurable()
    client.app.state.durable_workflows = scheduler
    session, agent, workflow = seed(client, selection, retired)
    response = client.post(f"/api/workflows/{workflow.name}/run", json={
        "sessionId": session["id"], "input": "6*7", "durable": durable,
        **({"idempotencyKey": "a" * 32} if durable else {}),
    })
    if retired:
        assert response.status_code == 422, response.text
        assert "generate_video is retired" in response.json()["detail"]
        assert "save" in response.json()["detail"]
        assert calls == [] and scheduler.scheduled == []
        assert client.get(f"/api/sessions/{session['id']}/messages").json() == []
    elif durable:
        assert response.status_code == 202, response.text
        assert len(scheduler.scheduled) == 1 and calls == []
    else:
        assert response.status_code == 200 and response.json()["ok"], response.text
        assert len(calls) == 3
        assert any(message["role"] == "tool" for message in calls[1]["messages"])
    assert_unchanged(client, agent, workflow)


@pytest.mark.parametrize("selection", ["agent", "step"])
@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("operation", ["runs", "schedules"])
@pytest.mark.parametrize("allow_tools", [False, True])
def test_automation_refuses_retired_selection_before_admission_or_schedule_activation(
    client, selection, retired, operation, allow_tools,
):
    automation, calls, _ = install_retirement(client)
    session, agent, workflow = seed(client, selection, retired)
    now = datetime.now(timezone.utc)
    body = {
        "selection": {"name": workflow.name, "model": "gpt-5.4"},
        "input": "6*7", "limits": {"spendMode": "no_hard_dollar_cap"},
        "idempotencyKey": now.isoformat().replace("+00:00", "Z") + "~" + "a" * 32,
        "allowTools": allow_tools,
    }
    if operation == "runs":
        body["sessionId"] = session["id"]
    else:
        due = (now + timedelta(minutes=5)).replace(second=0, microsecond=0)
        body["rule"] = {
            "frequency": "once", "timezone": "UTC", "localDate": due.date().isoformat(),
            "localTime": due.time().isoformat(), "maxOccurrences": 1,
        }
    response = client.post(f"/api/workflows/automation/{operation}", json=body)
    if retired:
        assert response.status_code == 422, response.text
        assert "generate_video is retired" in response.text
        assert calls == [] and automation.host.started == [] and automation.host.schedules == []
        assert automation.store.owners == {} and automation.store.schedules == {}
    else:
        assert response.status_code == (202 if operation == "runs" else 201), response.text
        if operation == "runs":
            assert len(automation.host.started) == 1
            result = client.portal.call(
                automation.advance, session["userId"], response.json()["runId"],
            )
            if result["status"] == "running":
                result = client.portal.call(
                    automation.advance, session["userId"], response.json()["runId"],
                )
            assert result["status"] == "completed", result
            assert calls
            if allow_tools:
                assert any(
                    message["role"] == "tool" for call in calls for message in call["messages"]
                )
        else:
            assert response.json()["status"] == "active"
            assert len(automation.host.schedules) == 1 and calls == []
    assert_unchanged(client, agent, workflow)


@pytest.mark.parametrize("selection", ["agent", "step"])
@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("single_step", [False, True])
def test_worker_execution_refuses_retired_frozen_step_before_model_call(
    client, selection, retired, single_step,
):
    _, calls, _ = install_retirement(client)
    _, agent, workflow = seed(client, selection, retired)
    state = client.app.state
    composed = client.portal.call(state.agent_service.catalog_for, agent.userId, state.agents)
    kwargs = dict(
        composed=composed, deployment=state.catalog.resolve_deployment("gpt-5.4").deploymentName,
        gateway=state.gateway, registry=state.tool_registry, executor=state.tool_executor,
        run_input="6*7",
    )
    if single_step:
        outcome = client.portal.call(partial(
            run_workflow_step, workflow.steps[1], index=1, workflow_name=workflow.name,
            previous="42", **kwargs,
        ))
        ok, error = outcome.result.ok, outcome.result.error
    else:
        result = client.portal.call(partial(run_workflow, workflow, **kwargs))
        ok, error = result.ok, result.text
    if retired:
        assert not ok and "generate_video is retired" in error
        assert calls == []
    else:
        assert ok and calls
    assert_unchanged(client, agent, workflow)


@pytest.mark.parametrize("selection", ["agent", "step"])
@pytest.mark.parametrize("retired", [False, True])
def test_durable_payload_cannot_freeze_retired_tool_selection(client, selection, retired):
    _, calls, _ = install_retirement(client)
    session, agent, workflow = seed(client, selection, retired)
    state = client.app.state
    composed = client.portal.call(state.agent_service.catalog_for, agent.userId, state.agents)
    build = partial(
        build_orchestration_payload, workflow, user_id=agent.userId, session_id=session["id"],
        run_input="6*7", model_id="gpt-5.4",
        deployment=state.catalog.resolve_deployment("gpt-5.4"),
        correlation_id=None, agent_snapshot=composed,
    )
    if retired:
        with pytest.raises(WorkflowValidationError, match="generate_video is retired"):
            build()
    else:
        payload = build()
        assert payload["context"]["agentSnapshot"] == composed.model_dump(mode="json")
    assert calls == []


@pytest.mark.parametrize("selection", ["agent", "step"])
def test_pre_retirement_frozen_bundle_refuses_new_admission_and_pending_schedule_recovery(
    client, selection,
):
    automation, calls, _ = install_retirement(client)
    session, agent, workflow = seed(client, selection, False)
    user = AuthenticatedUser(
        internal_user_id=session["userId"], provider="dev", subject="test", issuer="local",
    )
    limits = ExecutionLimits(spendMode="no_hard_dollar_cap")
    bundle = client.portal.call(partial(
        automation.access.freeze, user, WorkflowSelection(name=workflow.name, model="gpt-5.4"),
        limits, session_id=session["id"], safe_only=False,
    ))
    if selection == "agent":
        bundle.agents.get(agent.name).tools.append(TOOL)
    else:
        bundle.workflow.steps[1].extraTools.append(TOOL)
    key = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z") + "~" + "b" * 32
    with pytest.raises(AutomationError, match="generate_video is retired"):
        client.portal.call(partial(
            automation.start, bundle, "6*7", limits, key, user=user, session_id=session["id"],
        ))
    assert automation.store.owners == {} and automation.host.started == [] and calls == []

    from tests.test_workflow_schedule_service import prepare

    # A pending schedule can outlive a deployment; recovery must not activate its
    # old frozen tool selection even if its model/tool contracts are omitted.
    other, schedules, _, saved, _, other_calls = prepare(client)
    pending = saved.model_copy(update={"status": "pending"}, deep=True)
    if selection == "agent":
        pending.bundle.agents.agents[0].tools.append(TOOL)
    else:
        pending.bundle.workflow.steps[0].extraTools.append(TOOL)
    previous_starts = list(other.host.schedules)
    with pytest.raises(AutomationError, match="generate_video is retired"):
        client.portal.call(schedules.ensure_started, pending)
    assert other.host.schedules == previous_starts and other_calls == []
