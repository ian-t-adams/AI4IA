"""Owner propagation through actual API, nested runs, stream retry and workers."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec
from ai4ia_api.agents.orchestration import DELEGATE_TOOL_NAME, build_delegate_capability
from ai4ia_api.agents.runtime import AgentRunFailed
from ai4ia_api.agents.tool_exec import ToolContext, build_tools
from ai4ia_api.entitlements.models import EntitlementLimits
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.hard_quota.coverage import AttemptEnvelope
from ai4ia_api.hard_quota.dispatch import admission_scope
from ai4ia_api.hard_quota.models import QuotaError
from ai4ia_api.main import create_app
from ai4ia_api.model_evidence import ModelCallRecorder
from ai4ia_api.routers.realtime import DEV_SUBPROTOCOL, UpstreamMessage
from ai4ia_api.workflows.durable import DurableWorkflowService
from ai4ia_api.workflows.models import Workflow, WorkflowStep
from ai4ia_api.workflows.runner import run_workflow
from tests.conftest import make_settings
from tests.test_hard_quota_dispatch import DEPLOYMENT, Harness, response_for
from tests.test_model_call_evidence import transport_response
from tests.test_realtime_api import (
    ScriptedRealtimeConnector, _client as voice_client, _origin, _speech_client,
)


def leaf(name="helper"):
    return AgentSpec(name=name, displayName=name, description="", systemPrompt="Answer the task.")


async def test_delegated_child_uses_same_owner_and_cannot_escape_quota():
    h = Harness()
    sent = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (sent.append(req) or response_for("chat", req)),
    )) as http:
        registry, executor = build_tools()
        gateway = ModelGatewayClient(h.settings, http)
        _, handlers, usage = build_delegate_capability(
            orchestrator=leaf("boss").model_copy(update={"links": ["helper"]}),
            composed=AgentCatalog(agents=[leaf()]), gateway=gateway,
            registry=registry, executor=executor, deployment=DEPLOYMENT,
            model_id="fixture-text", pricing=h.pricing,
        )
        await h.limits(requestsPerMinute=0)
        with admission_scope(h.controller, "alice"):
            with pytest.raises((AgentRunFailed, QuotaError)):
                await handlers[DELEGATE_TOOL_NAME]({"agent": "helper", "task": "hello"}, ToolContext())
        assert sent == []
        await h.limits(requestsPerMinute=1)
        with admission_scope(h.controller, "alice"):
            result = await handlers[DELEGATE_TOOL_NAME](
                {"agent": "helper", "task": "hello"}, ToolContext(),
            )
        assert result["answer"] == "hello"
        assert len(sent) == len(usage) == 1
        assert not (await h.store.read("bob")).state.entries
        assert result.trace.model_evidence.snapshot()[0].admissions


async def test_in_request_workflow_rechecks_each_step_not_just_router_entry():
    h = Harness()
    sent = []
    workflow = Workflow(
        id="flow", userId="alice", name="flow", displayName="Flow",
        steps=[WorkflowStep(agent="helper", instruction="{input}"),
               WorkflowStep(agent="helper", instruction="{previous}")],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (sent.append(req) or response_for("chat", req)),
    )) as http:
        gateway = ModelGatewayClient(h.settings, http)
        registry, executor = build_tools()
        async def run():
            with admission_scope(h.controller, "alice"):
                return await run_workflow(
                    workflow, run_input="hello", composed=AgentCatalog(agents=[leaf()]),
                    deployment=DEPLOYMENT, gateway=gateway, registry=registry, executor=executor,
                    model_id="fixture-text", pricing=h.pricing,
                )
        await h.limits(requestsPerMinute=1)
        denied = await run()
        assert not denied.ok and len(sent) == 1
        await h.limits(requestsPerMinute=3)
        allowed = await run()
        assert allowed.ok and len(sent) == 3
        assert all(step.receipt.runtime.modelCalls[0].admissions for step in allowed.steps)


async def test_durable_thread_replay_refuses_hard_dispatch_and_soft_uses_persisted_owner():
    h = Harness()
    sent = []
    registry, executor = build_tools()
    composed = AgentCatalog(agents=[leaf()])
    async def catalog_for(owner, _curated):
        assert owner == "alice"
        return composed

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (sent.append(req) or response_for("chat", req)),
    )) as http:
        state = SimpleNamespace(
            settings=h.settings, hard_quota=h.controller, agents=composed,
            agent_service=SimpleNamespace(catalog_for=catalog_for),
            gateway=ModelGatewayClient(h.settings, http),
            tool_registry=registry, tool_executor=executor, usage=SimpleNamespace(pricing=h.pricing),
        )
        service = DurableWorkflowService(endpoint="", task_hub="", app_state=state)
        service._loop = asyncio.get_running_loop()
        activity = service._build_step_activity()
        payload = {
            "index": 0, "step": WorkflowStep(agent="helper", instruction="{input}").model_dump(),
            "context": {"userId": "alice", "sessionId": "session", "workflowName": "flow",
                        "runInput": "hello", "modelId": "fixture-text", "deployment": DEPLOYMENT},
        }
        with admission_scope(h.controller, "bob"):
            denied = await asyncio.to_thread(activity, None, payload)
        assert denied["fatal"] and "replay identity" in denied["result"]["error"]
        assert sent == []
        h.settings.hard_quota_enabled = False
        h.controller.enabled = False
        state.gateway = ModelGatewayClient(h.settings, http)
        await h.entitlements.set("bob", EntitlementLimits(disabled=True), updated_by=None)
        with admission_scope(h.controller, "bob"):
            allowed = await asyncio.to_thread(activity, None, payload)
        assert not allowed["fatal"] and len(sent) == 1
        await h.limits(disabled=True)
        denied = await asyncio.to_thread(activity, None, payload)
        assert denied["fatal"] and denied["result"]["error"] == "This account is disabled."
        assert len(sent) == 1


async def test_stream_parameter_fallback_consumes_an_independent_dispatch_reservation():
    h = Harness()
    sent = []
    def transport(req):
        sent.append(json.loads(req.content))
        if sent[-1].get("stream_options"):
            return httpx.Response(400, text="unsupported stream_options")
        return response_for("chat-stream", req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        gateway = ModelGatewayClient(h.settings, http)
        async def stream():
            with admission_scope(h.controller, "alice"):
                return [chunk async for chunk in gateway.stream(
                    deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}],
                )]
        await h.limits(requestsPerMinute=1)
        with pytest.raises(QuotaError, match="would be exceeded"):
            await stream()
        assert len(sent) == 1
        await h.limits(requestsPerMinute=3)
        assert (await stream())[-1].done
        assert len(sent) == 3
        state = (await h.store.read("alice")).state
        assert len(state.entries) == 3
        assert sum(record.phase == "unknown" for record in state.entries.values()) == 2


@pytest.mark.parametrize("kind", ["cancel", "timeout", "partial", "unknown"])
async def test_actual_dispatch_ambiguity_never_refunds_the_fixture_bound(kind):
    h = Harness()
    h.controller.attempts = AttemptEnvelope("fixture-single-send-v1", 1)
    await h.limits(tokensPerDay=120)
    sent = []
    async def transport(req):
        sent.append(req)
        if kind == "cancel":
            raise asyncio.CancelledError
        if kind == "timeout":
            raise httpx.ReadTimeout("fixture timeout")
        if kind == "partial":
            return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 3}})
        return httpx.Response(200, json={"choices": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        gateway = ModelGatewayClient(h.settings, http)
        with admission_scope(h.controller, "alice"):
            if kind in {"cancel", "timeout"}:
                from ai4ia_api.gateway.client import ModelGatewayError
                with pytest.raises((asyncio.CancelledError, ModelGatewayError)):
                    await gateway.complete(
                        deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}],
                    )
            else:
                await gateway.complete(
                    deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}],
                )
        record = next(iter((await h.store.read("alice")).state.entries.values()))
        assert record.phase == "unknown" and record.charged.tokens == 120
        with admission_scope(h.controller, "alice"):
            with pytest.raises(QuotaError, match="would be exceeded"):
                await gateway.complete(
                    deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}],
                )
        assert len(sent) == 1


async def test_actual_payload_is_frozen_before_policy_and_coordination_awaits():
    h = Harness()
    messages = [{"role": "user", "content": "original"}]
    original_read = h.store.read
    async def mutating_read(owner):
        messages[0]["content"] = "changed during coordination"
        return await original_read(owner)
    h.store.read = mutating_read
    sent = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (sent.append(json.loads(req.content)) or response_for("chat", req)),
    )) as http:
        with admission_scope(h.controller, "alice"):
            await ModelGatewayClient(h.settings, http).complete(deployment=DEPLOYMENT, messages=messages)
    assert messages[0]["content"] == "changed during coordination"
    assert sent[0]["messages"][0]["content"] == "original"


async def test_cosmos_adapter_storage_outage_prevents_actual_gateway_dispatch():
    from ai4ia_api.hard_quota.cosmos_store import CosmosReservationStore
    from ai4ia_api.hard_quota.service import ReservationService
    from tests.test_hard_quota_reservations import StatefulContainer

    h = Harness()
    container = StatefulContainer(lambda: h.clock[0])
    container.seed((await h.store.read("alice")).state)
    async def account():
        return {"enableMultipleWriteLocations": False, "writableLocations": [{}]}
    h.controller.reservations = ReservationService(CosmosReservationStore(
        container, read_account=account,
    ))
    sent = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (sent.append(req) or response_for("chat", req)),
    )) as http:
        gateway = ModelGatewayClient(h.settings, http)
        async def invoke():
            with admission_scope(h.controller, "alice"):
                return await gateway.complete(
                    deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}],
                )
        container.fail = True
        with pytest.raises(QuotaError, match="unavailable"):
            await invoke()
        assert sent == []
        container.fail = False
        assert await invoke()
        assert len(sent) == 1


async def test_settlement_failure_keeps_dispatched_evidence_and_never_refunds():
    h = Harness()
    sent = []
    recorder = ModelCallRecorder()
    def transport(req):
        sent.append(req)
        h.store.available = False
        return response_for("chat", req)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        gateway = ModelGatewayClient(h.settings, http)
        with admission_scope(h.controller, "alice") as context:
            with pytest.raises(QuotaError, match="unavailable"):
                await recorder.observe(gateway.complete(
                    deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}],
                ))
        assert len(sent) == 1
        assert context.evidence[0].phase == "dispatched"
        assert recorder.snapshot()[0].admissions[0].phase == "dispatched"
        h.store.available = True
        record = next(iter((await h.store.read("alice")).state.entries.values()))
        assert record.phase == "dispatched" and record.charged.requests == 1


async def test_compute_cap_is_one_application_sandbox_dispatch_with_control():
    from ai4ia_api.code_interpreter.client import CodeInterpreterClient

    h = Harness()
    sent = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (sent.append(req) or response_for("compute", req)),
    )) as http:
        client = CodeInterpreterClient(h.settings, http_client=http)
        await h.limits(computeExecutionsPerDay=0)
        with admission_scope(h.controller, "alice"):
            with pytest.raises(QuotaError, match="computeExecutionsPerDay"):
                await client.run(instructions="calculate", user_input="hello")
        assert sent == []
        await h.limits(computeExecutionsPerDay=1)
        with admission_scope(h.controller, "alice") as context:
            assert (await client.run(instructions="calculate", user_input="hello")).succeeded
        assert len(sent) == 1 and context.evidence[0].charged.compute == 1


@pytest.mark.parametrize("stream", [False, True])
def test_authenticated_api_binds_owner_and_persists_bounded_admission_evidence(stream):
    settings = make_settings(hard_quota_enabled=True, entitlements_enabled=False, admin_subjects="alice")
    app = create_app(settings)
    sent = []
    def transport(req):
        body = json.loads(req.content)
        sent.append(body)
        return transport_response(body, {
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })
    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    headers = {"X-Dev-User": "alice"}
    try:
        with TestClient(app) as client:
            app.state.gateway = ModelGatewayClient(settings, http)
            model = next(m for m in app.state.catalog.conversational_models() if m.api == "chat")
            uid = client.get("/api/entitlement", headers=headers).json()["userId"]
            store = app.state.hard_quota.reservations.store
            assert not store._rows  # no seed merely because an owner authenticated
            sid = client.post("/api/sessions", headers=headers, json={"model": model.id}).json()["id"]
            # Missing state is unavailable, not zero consumption.
            client.post("/api/chat", headers=headers, json={
                "sessionId": sid, "content": "hello", "stream": stream,
            })
            assert sent == []
            store.seed(uid)
            response = client.post("/api/chat", headers=headers, json={
                "sessionId": sid, "content": "hello", "stream": stream,
            })
            assert response.status_code == 200, response.text
            assert len(sent) == 1
            messages = client.get(f"/api/sessions/{sid}/messages", headers=headers).json()
            receipt = [row["executionReceipt"] for row in messages if row["role"] == "assistant"][-1]
            admission = receipt["runtime"]["modelCalls"][0]["admissions"][0]
            assert admission["charged"]["requests"] == 1
            assert admission["reserved"]["tokens"] is None  # no shipping retry envelope
            assert "hello" not in json.dumps(admission)
            assert len(json.dumps(receipt, ensure_ascii=True).encode("utf-8")) <= 32768
            records = app.state.usage._repo._by_user[uid]
            assert records[-1].hardQuota[0].operationHash == admission["operationHash"]
            assert client.get(
                f"/api/sessions/{sid}/messages", headers={"X-Dev-User": "bob"},
            ).status_code == 404
    finally:
        asyncio.run(http.aclose())


@pytest.mark.parametrize("provider", ["azure_openai", "speech_voice_live"])
@pytest.mark.parametrize("denial", ["requests", "tokens", "dollars", "storage"])
def test_realtime_connect_is_guarded_for_each_provider_with_allowed_control(provider, denial):
    factory = _speech_client if provider == "speech_voice_live" else voice_client
    client = factory(
        realtime_enabled=True, hard_quota_enabled=True, entitlements_enabled=False,
    )
    try:
        headers = {"X-Dev-User": "alice"}
        uid = client.get("/api/entitlement", headers=headers).json()["userId"]
        store = client.app.state.hard_quota.reservations.store
        if denial != "storage":
            store.seed(uid)
        cap = {"requests": {"requestsPerMinute": 0}, "tokens": {"tokensPerDay": 1000},
               "dollars": {"costPerDayMicroUsd": 1000}, "storage": {}}[denial]
        client.put(f"/api/admin/entitlements/{uid}", headers=headers, json=cap)
        connector = ScriptedRealtimeConnector([UpstreamMessage("close", close_code=1000)])
        client.app.state.realtime_connector = connector
        def connect():
            with client.websocket_connect(
                f"/api/voice/live?provider={provider}", subprotocols=[DEV_SUBPROTOCOL, "alice"],
                headers=_origin(),
            ) as websocket:
                for _ in range(10):
                    if websocket.receive()["type"] == "websocket.close":
                        return
                raise AssertionError("fixture relay did not terminate")
        connect()
        assert connector.connects == []
        if denial == "storage":
            store.seed(uid)
        client.put(f"/api/admin/entitlements/{uid}", headers=headers, json={"requestsPerMinute": 1})
        connect()
        assert len(connector.connects) == 1
    finally:
        client.__exit__(None, None, None)
