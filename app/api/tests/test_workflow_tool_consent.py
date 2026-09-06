"""Workflow consent is an invocation-scoped opt-in, never an unattended exemption."""
from __future__ import annotations

import json
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest

from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec
from ai4ia_api.agents.tool_exec import build_tools
from ai4ia_api.workflows.models import Workflow, WorkflowStep
from ai4ia_api.workflows.runner import run_workflow
from tests.test_agent_runtime import ScriptedGateway, _assistant_text, _assistant_tool_call
from tests.test_tool_approval_gate import _InjectedModelGateway, _bootstrap, _client, _connector


async def test_unattended_unapproved_call_is_visible_failed_step():
    calls = []

    async def browse(args, _ctx):
        calls.append(args)
        return {"content": "remote output"}

    registry, executor = build_tools()
    gateway = ScriptedGateway([
        _assistant_tool_call("c1", "browse_url", json.dumps({"url": "https://example.org"})),
        _assistant_text("I cannot do that without approval."),
    ])
    workflow = Workflow(
        id="research", userId="user", name="research", displayName="Research",
        steps=[WorkflowStep(agent="researcher", instruction="{input}")],
    )
    composed = AgentCatalog(agents=[AgentSpec(
        name="researcher", displayName="Researcher", description="Research",
        systemPrompt="Research the request.",
    )])
    outcome = await run_workflow(
        workflow, run_input="Read a page", composed=composed, deployment="model",
        gateway=gateway, registry=registry, executor=executor,
        capabilities=lambda _names: (
            [{"type": "function", "function": {
                "name": "browse_url", "parameters": {"type": "object"},
            }}],
            {"browse_url": browse},
        ),
    )
    assert outcome.ok is False
    assert not calls
    assert "approval" in outcome.text.lower()
    receipt = outcome.steps[0].receipt
    assert receipt is not None
    assert receipt.toolCallCount == 1
    assert receipt.toolCalls[0].outcome == "denied"
    assert receipt.toolCalls[0].arguments is not None
    assert receipt.approvalsRequested == 1


def _workflow(client):
    response = client.post("/api/workflows", json={
        "name": "send", "steps": [{"agent": "courierbot", "instruction": "{input}"}],
    })
    assert response.status_code == 201, response.text


def test_run_opt_in_is_not_inherited_from_session_and_receipts_survive():
    connector = _connector()
    client = _client(connector, tool_auto_approve_enabled=True)
    try:
        sid = _bootstrap(client)
        client.patch(f"/api/sessions/{sid}", json={"agentName": "courierbot"})
        assert client.post(
            f"/api/sessions/{sid}/tool-consent", json={"enabled": True},
        ).status_code == 200
        _workflow(client)
        assert client.get("/api/workflows").json()["toolAutoApproveAvailable"] is True
        for enabled in (False, True, False):
            connector.tool_calls.clear()
            client.app.state.gateway = _InjectedModelGateway(repeat=2)
            response = client.post("/api/workflows/send/run", json={
                "sessionId": sid, "input": "Send the update", "autoApproveTools": enabled,
                "idempotencyKey": "consented-direct-run" if enabled else None,
            })
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["ok"] is enabled
            assert body["autoApproveTools"] is enabled
            assert len(connector.tool_calls) == (2 if enabled else 0)
            message = body["message"]
            assert "workflowToolConsentState" not in message
            assert message["executionReceipt"]["toolCallCount"] == 2
            assert len(message["workflowStepReceipts"]) == 1
            receipt = message["workflowStepReceipts"][0]
            assert receipt["prompt"] and receipt["modelRequests"]
            assert len(message["steps"]) == 3
            if enabled:
                grant = message["workflowToolConsent"]
                assert grant["scope"] == "run"
                assert all(call["approval"] == "run" for call in receipt["toolCalls"])
                assert all(call["consentId"] == grant["id"] for call in receipt["toolCalls"])
            else:
                assert message["workflowToolConsent"] is None
                assert message["status"] == "error"
        messages = client.get(f"/api/sessions/{sid}/messages").json()
        assert [message["role"] for message in messages] == ["user", "assistant"] * 3
        assert messages[-1]["workflowStepReceipts"] == body["message"]["workflowStepReceipts"]
    finally:
        client.__exit__(None, None, None)


def test_workflow_operator_opt_out_stays_distinct_from_user_consent():
    connector = _connector()
    client = _client(connector, tool_approval_mode="off")
    try:
        sid = _bootstrap(client)
        _workflow(client)
        client.app.state.gateway = _InjectedModelGateway()
        response = client.post("/api/workflows/send/run", json={
            "sessionId": sid, "input": "Update",
        })
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert len(connector.tool_calls) == 1
        message = response.json()["message"]
        assert message["workflowToolConsent"] is None
        assert message["executionReceipt"]["toolCalls"][0]["approval"] == "operator"
    finally:
        client.__exit__(None, None, None)


def test_direct_run_can_be_cancelled_by_known_key_before_run_id_is_returned():
    connector = _connector()
    client = _client(connector, tool_auto_approve_enabled=True)
    entered = threading.Event()
    release = threading.Event()

    class PausedGateway(_InjectedModelGateway):
        async def complete(self, **kwargs):
            if not self.calls:
                entered.set()
                assert await asyncio.to_thread(release.wait, 10)
            return await super().complete(**kwargs)

    try:
        sid = _bootstrap(client)
        _workflow(client)
        client.app.state.gateway = PausedGateway(repeat=2)
        body = {
            "sessionId": sid, "input": "Update", "autoApproveTools": True,
            "idempotencyKey": "known-before-start",
        }
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(client.post, "/api/workflows/send/run", json=body)
            try:
                assert entered.wait(5), "The server never entered the model call."
                assert not running.done(), "The synchronous response must still be pending."
                forbidden = client.post("/api/workflows/send/cancel", json={
                    "sessionId": sid, "idempotencyKey": body["idempotencyKey"],
                }, headers={"X-Dev-User": "other"})
                assert forbidden.status_code == 404
                unrelated = client.post("/api/workflows/send/cancel", json={
                    "sessionId": sid, "idempotencyKey": "different-run-key",
                })
                assert unrelated.status_code == 404
                cancelled = client.post("/api/workflows/send/cancel", json={
                    "sessionId": sid, "idempotencyKey": body["idempotencyKey"],
                })
                assert cancelled.status_code == 200, cancelled.text
                assert cancelled.json()["status"] == "TERMINATED"
            finally:
                release.set()
            finished = running.result(timeout=10)
        assert finished.status_code == 200, finished.text
        assert finished.json()["ok"] is False
        message = finished.json()["message"]
        assert message["status"] == "cancelled"
        assert message["executionReceipt"]["toolCallCount"] == 2
        assert all(call["outcome"] == "denied" for call in message["executionReceipt"]["toolCalls"])
        assert connector.tool_calls == []
        client.app.state.gateway = _InjectedModelGateway()
        duplicate = client.post("/api/workflows/send/run", json=body)
        assert duplicate.status_code == 409
        changed_approval = client.post(
            "/api/workflows/send/run", json={**body, "autoApproveTools": False},
        )
        assert changed_approval.status_code == 409
        assert connector.tool_calls == []
        saved = client.get(f"/api/sessions/{sid}/messages").json()
        assert len(saved) == 2
        assert saved[-1]["workflowToolConsent"] == message["workflowToolConsent"]
    finally:
        release.set()
        client.__exit__(None, None, None)


def test_direct_auto_approval_requires_a_caller_known_cancellation_handle():
    client = _client(_connector(), tool_auto_approve_enabled=True)
    try:
        sid = _bootstrap(client)
        _workflow(client)
        client.app.state.gateway = _InjectedModelGateway()
        response = client.post("/api/workflows/send/run", json={
            "sessionId": sid, "input": "Update", "autoApproveTools": True,
        })
        assert response.status_code == 422
        assert "idempotencyKey" in response.text
        assert client.get(f"/api/sessions/{sid}/messages").json() == []
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("race", [False, True])
def test_cancellation_preserves_a_new_checkpoint_with_the_same_status(client, race):
    from ai4ia_api.workflows.durable import durable_message_ids
    from tests.test_tool_consent_storage import _checkpoint

    sid = client.post("/api/sessions", json={"model": "gpt-5.4"}).json()["id"]
    repo = client.app.state.session_repo
    session = repo._sessions[sid]
    initial = _checkpoint(session, 1)
    _, initial.id = durable_message_ids(initial.workflowRunId)
    repo._messages[sid] = [initial]
    newer = _checkpoint(session, 2).model_copy(update={"id": initial.id, "createdAt": initial.createdAt})
    replace = repo.replace_message_if_workflow_status
    attempts = []

    async def checkpoint_before_replace(uid, message, **kwargs):
        if race and not attempts:
            await repo.upsert_message(uid, newer)
        attempts.append(kwargs)
        return await replace(uid, message, **kwargs)

    repo.replace_message_if_workflow_status = checkpoint_before_replace
    response = client.post(
        f"/api/workflows/runs/{initial.workflowRunId}/cancel", json={"sessionId": sid},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "TERMINATED"
    saved = client.get(f"/api/sessions/{sid}/messages").json()[-1]
    assert saved["workflowConsentRevoked"] is True
    assert saved["status"] == "cancelled"
    assert len(saved["workflowStepReceipts"]) == (2 if race else 1)
    assert saved["executionReceipt"]["toolCallCount"] == (2 if race else 1)
    assert len(attempts) == (2 if race else 1)


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_workflow_preserves_completed_tool_receipt(cancel):
    registry, executor = build_tools()
    responses = [
        _assistant_tool_call("c1", "calculator", '{"expression":"2+3"}'),
    ]

    class FailingGateway(ScriptedGateway):
        async def complete(self, **kwargs):
            if self.calls:
                if cancel:
                    raise asyncio.CancelledError()
                raise RuntimeError("provider failure")
            return await super().complete(**kwargs)

    workflow = Workflow(
        id="calculate", userId="user", name="calculate", displayName="Calculate",
        steps=[WorkflowStep(agent="calculator", instruction="{input}")],
    )
    result = await run_workflow(
        workflow, run_input="Calculate", composed=AgentCatalog(agents=[AgentSpec(
            name="calculator", displayName="Calculator", description="Calculate",
            systemPrompt="Use the calculator.", tools=["calculator"],
        )]),
        deployment="m", gateway=FailingGateway(responses), registry=registry, executor=executor,
    )
    assert result.ok is False
    assert result.cancelled is cancel
    receipt = result.steps[0].receipt
    assert receipt is not None
    assert receipt.toolCallCount == 1
    assert receipt.toolCalls[0].arguments is not None
    assert '"result":5' in receipt.toolCalls[0].result.text
    assert receipt.modelRequests
    assert receipt.status == ("cancelled" if cancel else "error")
    assert receipt.partial


class _CaptureDurable:
    def __init__(self):
        self.payloads = []

    async def schedule(self, payload, **kwargs):
        self.payloads.append(payload)
        return kwargs["run_id"]


def test_durable_retry_retains_exact_consent_and_cannot_change_approval():
    client = _client(_connector(), tool_auto_approve_enabled=True)
    try:
        sid = _bootstrap(client)
        _workflow(client)
        durable = _CaptureDurable()
        client.app.state.durable_workflows = durable
        body = {
            "sessionId": sid, "input": "Update", "durable": True,
            "idempotencyKey": "same-request-key", "autoApproveTools": True,
        }
        first = client.post("/api/workflows/send/run", json=body)
        assert first.status_code == 202, first.text
        repeated = client.post("/api/workflows/send/run", json=body)
        assert repeated.status_code == 202, repeated.text
        assert repeated.json()["toolConsent"] == first.json()["toolConsent"]
        assert len(durable.payloads) == 1
        context = durable.payloads[0]["context"]
        assert context["toolConsent"]["grant"]["id"] == first.json()["toolConsent"]["id"]
        assert context["agentSnapshot"]["agents"][0]["name"] == "courierbot"
        assert client.post(
            "/api/workflows/send/run", json={**body, "autoApproveTools": False},
        ).status_code == 409
        assert client.post(
            "/api/workflows/send/run", json={**body, "toolConsent": context["toolConsent"]},
        ).status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_frozen_workflow_input_is_bounded_before_scheduling():
    from ai4ia_api.catalog import DeploymentOption
    from ai4ia_api.workflows.durable import (
        MAX_ORCHESTRATION_INPUT_BYTES, WorkflowPayloadTooLargeError,
        build_orchestration_payload,
    )

    workflow = Workflow(
        id="flow", userId="user", name="flow", displayName="Flow",
        steps=[WorkflowStep(agent="a", instruction="{input}")],
    )
    kwargs = dict(
        user_id="user", session_id="session", model_id="m", correlation_id=None,
        deployment=DeploymentOption(
            deploymentName="model", region="eastus2", sku="GlobalStandard", dataZone="US",
        ),
    )
    assert build_orchestration_payload(workflow, run_input="small", **kwargs)["context"]
    with pytest.raises(WorkflowPayloadTooLargeError):
        build_orchestration_payload(
            workflow, run_input="x" * MAX_ORCHESTRATION_INPUT_BYTES, **kwargs,
        )


@pytest.mark.parametrize("change", [None, "disable", "cancel", "agent", "entitlement"])
def test_durable_worker_checks_live_consent_and_preserves_execution(change):
    from ai4ia_api.workflows.durable import DurableWorkflowService

    connector = _connector()
    client = _client(connector, tool_auto_approve_enabled=True)
    try:
        sid = _bootstrap(client)
        _workflow(client)
        durable = _CaptureDurable()
        client.app.state.durable_workflows = durable
        body = {
            "sessionId": sid, "input": "Update", "durable": True,
            "idempotencyKey": "worker-run-key", "autoApproveTools": True,
        }
        accepted = client.post("/api/workflows/send/run", json=body)
        assert accepted.status_code == 202, accepted.text
        run_id = accepted.json()["runId"]
        if change == "disable":
            client.app.state.settings.tool_auto_approve_enabled = False
        elif change == "cancel":
            forbidden = client.post(
                f"/api/workflows/runs/{run_id}/cancel",
                json={"sessionId": sid}, headers={"X-Dev-User": "other-user"},
            )
            assert forbidden.status_code == 404
            cancelled = client.post(
                f"/api/workflows/runs/{run_id}/cancel", json={"sessionId": sid},
            )
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "TERMINATED"
            assert client.get(
                f"/api/workflows/runs/{run_id}", params={"sessionId": sid},
            ).json()["status"] == "TERMINATED"
        elif change == "agent":
            assert client.put("/api/agents/courierbot", json={
                "systemPrompt": "New configuration", "tools": ["mcp:courier/send"],
            }).status_code == 200
        elif change == "entitlement":
            from ai4ia_api.entitlements.models import EntitlementLimits

            uid = client.get(f"/api/sessions/{sid}").json()["userId"]
            client.portal.call(partial(
                client.app.state.entitlements.set,
                uid, EntitlementLimits(disabled=True), updated_by="test",
            ))
        client.app.state.gateway = _InjectedModelGateway(repeat=2)
        payload = durable.payloads[0]
        service = DurableWorkflowService(
            endpoint="https://scheduler.example.org", task_hub="test",
            app_state=client.app.state,
        )
        outcome = client.portal.call(partial(
            service._execute_step,
            step=WorkflowStep.model_validate(payload["steps"][0]),
            index=0, previous="", context=payload["context"],
        ))
        assert len(connector.tool_calls) == (2 if change is None else 0)
        assert outcome["fatal"] is (change is not None)
        client.portal.call(partial(service._persist, {
            "context": payload["context"], "ok": not outcome["fatal"],
            "text": outcome["result"]["text"] or outcome["result"]["error"],
            "steps": [outcome["result"]], "usage": outcome["usage"],
        }))
        message = client.get(f"/api/sessions/{sid}/messages").json()[-1]
        assert message["executionReceipt"]["toolConsent"]["scope"] == "run"
        if change is None:
            assert message["executionReceipt"]["autoApprovedToolCalls"] == 2
            assert message["workflowStepReceipts"][0]["toolCalls"][0]["approval"] == "run"
        elif change == "cancel":
            assert message["status"] == "cancelled"
            assert message["workflowConsentRevoked"] is True
            assert message["executionReceipt"]["status"] == "cancelled"
        else:
            assert message["status"] == "error"
    finally:
        client.__exit__(None, None, None)
