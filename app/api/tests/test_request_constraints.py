"""Request reductions reach real offers, dispatch, memory IO, and provider adapters."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.approvals import ApprovalPolicy
from ai4ia_api.agents.runtime import run_agent_turn
from ai4ia_api.agents.synthetic_governance import synthetic_spec
from ai4ia_api.agents.tool_exec import ToolContext, ToolDefinition, ToolExecutionError, build_tools
from ai4ia_api.agents.tools import ToolSpec
from ai4ia_api.gateway.client import ModelGatewayClient, ModelGatewayError
from ai4ia_api.main import create_app
from ai4ia_api.memory.context import MemoryContextGuard
from ai4ia_api.memory.in_memory import InMemoryVectorStore
from ai4ia_api.memory.service import MemoryService
from ai4ia_api.request_constraints import (
    automatic_memory_allowed, constrain_request, tools_allowed,
)
from tests.conftest import make_settings
from tests.test_chat_memory_api import CapturingGateway
from tests.test_chat_websearch_api import FakeWebClient, ScriptedWebGateway, _inject_web
from tests.test_memory_preference import RecordingEmbedder, cosmos_memory, set_automatic
from tests.test_memory_preference_execution import RecordingGateway


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("allowed", [False, True])
def test_plain_request_reduction_controls_ambient_web_execution_and_receipts(stream, allowed):
    app = create_app(make_settings(tool_approval_mode="off"))
    with TestClient(app) as client:
        web = FakeWebClient()
        _inject_web(client, web)
        gateway = ScriptedWebGateway(call_tool=True)
        app.state.gateway = gateway
        created = client.post(
            "/api/sessions",
            json={"title": "Constraint fixture", "model": "gpt-5.2", "libraryDocumentIds": []},
        ).json()
        result = client.post("/api/chat", json={
            "sessionId": created["id"], "content": "A fixed non-sensitive request",
            "stream": stream, "allowTools": allowed, "allowAutomaticMemory": False,
        })
        assert result.status_code == 200, result.text
        assert bool(web.calls) is allowed
        assert gateway.tools_offered_first_call is allowed
        assert gateway.calls == (2 if allowed else 1)
        messages = client.get(f"/api/sessions/{created['id']}/messages").json()
        receipt = messages[-1]["executionReceipt"]
        assert bool(receipt["toolsOffered"]) is allowed
        assert bool(receipt["toolCalls"]) is allowed
        # A reduction lasts for this response, not the worker's next request.
        assert tools_allowed() and automatic_memory_allowed()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("backend", ["local", "cosmos"])
def test_request_memory_deny_preserves_preference_and_blocks_embeddings_planner_and_writes(stream, backend):
    app = create_app(make_settings())
    with TestClient(app) as client:
        if backend == "cosmos":
            memory, _store, embedder, planner, _container = cosmos_memory()
        else:
            embedder = RecordingEmbedder()
            planner = None
            memory = MemoryService(store=InMemoryVectorStore(), embedder=embedder)
        app.state.memory = memory
        app.state.gateway = CapturingGateway()
        session = client.post(
            "/api/sessions", json={"title": "Fixture", "model": "gpt-5.2", "libraryDocumentIds": []},
        ).json()
        uid = session["userId"]
        preference = client.get("/api/memories/preference").json()
        assert preference["automaticMemoryEnabled"] is True
        for allowed in (False, True):
            embedder.calls.clear()
            if planner is not None:
                planner.calls.clear()
            result = client.post("/api/chat", json={
                "sessionId": session["id"], "content": "A durable non-sensitive synthetic fact",
                "stream": stream, "allowTools": False, "allowAutomaticMemory": allowed,
            })
            assert result.status_code == 200, result.text
            assert bool(embedder.calls) is allowed
            if planner is not None:
                assert bool(planner.calls) is allowed
            assert client.get("/api/memories/preference").json() == preference
        assert uid


@pytest.mark.parametrize("kind", ["local", "cosmos"])
async def test_nested_reductions_and_current_preference_are_intersected_not_overridden(kind):
    if kind == "cosmos":
        memory, _store, embedder, _planner, _container = cosmos_memory()
    else:
        embedder = RecordingEmbedder()
        memory = MemoryService(store=InMemoryVectorStore(), embedder=embedder)
    with constrain_request(tools=False, automatic_memory=False):
        with constrain_request(tools=True, automatic_memory=True):
            assert not tools_allowed() and not automatic_memory_allowed()
            assert not await MemoryContextGuard(memory, "owner").allowed()
            assert await memory.recall("owner", "synthetic query") == []
            assert await memory.remember("owner", "session", "Long enough synthetic fact") == "disabled"
            assert embedder.calls == []
    assert tools_allowed() and automatic_memory_allowed()
    await set_automatic(memory, "owner", False)
    with constrain_request(tools=True, automatic_memory=True):
        assert await memory.recall("owner", "synthetic query") == []
        assert await memory.remember("owner", "session", "Long enough synthetic fact") == "disabled"
        assert embedder.calls == []
    await set_automatic(memory, "owner", True)
    assert await memory.remember("owner", "session", "Long enough synthetic fact") == "saved"
    assert embedder.calls


@pytest.mark.parametrize("content", ["/calculator 1+1", "/research a question", "/run_workflow workflow", "/help"])
def test_constrained_commands_refuse_before_a_user_message_or_dispatch(client, content):
    created = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
    result = client.post("/api/chat", json={
        "sessionId": created["id"], "content": content, "allowTools": False, "stream": False,
    })
    assert result.status_code == 422
    assert client.get(f"/api/sessions/{created['id']}/messages").json() == []


def test_selected_source_requiring_tools_is_not_silently_rewritten(client):
    created = client.post("/api/agents", json={
        "name": "requires-tools", "systemPrompt": "Calculate accurately.", "tools": ["calculator"],
    })
    assert created.status_code == 201
    session = client.post("/api/sessions", json={"model": "gpt-5.2", "agentName": "requires-tools"}).json()
    result = client.post("/api/chat", json={
        "sessionId": session["id"], "content": "Compute a value", "allowTools": False, "stream": False,
    })
    assert result.status_code == 422
    assert client.get(f"/api/sessions/{session['id']}/messages").json() == []
    assert client.get(f"/api/sessions/{session['id']}").json()["agentName"] == "requires-tools"


@pytest.mark.parametrize("key", ["allowTools", "allowAutomaticMemory", "requireFreshSession"])
@pytest.mark.parametrize("value", [0, 1, "false", None])
def test_constraint_booleans_are_strict_at_http_boundary(client, key, value):
    result = client.post("/api/chat", json={
        "sessionId": "not-created", "content": "hello", key: value,
    })
    assert result.status_code == 422


def test_require_fresh_session_refuses_context_and_second_turn_before_dispatch():
    app = create_app(make_settings(session_deletion_enabled=True))
    with TestClient(app) as client:
        _assert_fresh_turn(client)


def _assert_fresh_turn(client):
    gateway = CapturingGateway()
    client.app.state.gateway = gateway
    session = client.post(
        "/api/sessions", json={"model": "gpt-5.2", "libraryDocumentIds": [], "title": "Fixture"},
    ).json()
    body = {
        "sessionId": session["id"], "content": "fixed sentinel", "requireFreshSession": True,
        "allowTools": False, "allowAutomaticMemory": False, "stream": False,
    }
    assert client.post("/api/chat", json=body).status_code == 200
    assert gateway.calls == 1
    assert client.post("/api/chat", json=body).status_code == 409
    assert gateway.calls == 1
    for fields in ({}, {"libraryDocumentIds": [], "systemPrompt": "Other context"}):
        another = client.post("/api/sessions", json={"model": "gpt-5.2", **fields}).json()
        assert client.post("/api/chat", json={**body, "sessionId": another["id"]}).status_code == 409
    assert gateway.calls == 1


@pytest.mark.parametrize("tool", ["fixture_tool", "mcp:fixture/read", "load_skill"])
@pytest.mark.parametrize("allowed", [False, True])
async def test_registry_executor_and_runtime_apply_same_deny_at_actual_dispatch(tool, allowed):
    invoked = []

    async def handler(args, ctx):
        invoked.append(args)
        return {"ok": True}

    definition = ToolDefinition(
        spec=ToolSpec(tool, "Synthetic test tool"),
        parameters={"type": "object"}, handler=handler,
    )
    registry, executor = build_tools([definition])
    gateway = RecordingGateway(tools=(tool,))
    ctx = ToolContext(approval_policy=ApprovalPolicy.off)
    with constrain_request(tools=allowed, automatic_memory=False):
        result = await run_agent_turn(
            gateway=gateway, deployment="test-deployment",
            messages=[{"role": "user", "content": "Fixture"}],
            tool_names=[tool], registry=registry, executor=executor, ctx=ctx,
        )
        assert bool(invoked) is allowed
        assert bool(result.offered_tools) is allowed
        if not allowed:
            assert registry.authorize(tool, approved=True).reason.value == "request_restricted"
            with pytest.raises(ToolExecutionError):
                await executor.execute(tool, {}, ctx)
    assert tools_allowed()


@pytest.mark.parametrize("tool", ["web_search", "delegate_to_agent", "run_workflow", "fetch_document"])
@pytest.mark.parametrize("allowed", [False, True])
async def test_synthetic_dispatch_cannot_bypass_request_denial(tool, allowed):
    invoked = []

    async def handler(args, ctx):
        invoked.append(args)
        return {"ok": True}

    registry, executor = build_tools()
    spec = synthetic_spec(tool)
    assert spec is not None
    gateway = RecordingGateway(tools=(tool,))
    with constrain_request(tools=allowed, automatic_memory=False):
        result = await run_agent_turn(
            gateway=gateway, deployment="test-deployment",
            messages=[{"role": "user", "content": "Fixture"}],
            tool_names=[], registry=registry, executor=executor,
            ctx=ToolContext(
                approval_policy=ApprovalPolicy.off, granted_scopes=spec.scopes,
                approvals=frozenset({tool}),
            ),
            extra_tools=[{"type": "function", "function": {"name": tool, "parameters": {"type": "object"}}}],
            extra_handlers={tool: handler},
        )
    assert bool(invoked) is allowed
    assert bool(result.offered_tools) is allowed


def _provider_payload(api, stream):
    function = {"name": "calculator", "arguments": '{"expression":"1+1"}'}
    if api == "chat":
        call = {"id": "call-1", "type": "function", "function": function}
        if stream:
            return 'data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [call]}}]}) + "\n\ndata: [DONE]\n\n"
        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}}]}
    if api == "responses":
        item = {"type": "function_call", "call_id": "call-1", **function}
        if stream:
            return "data: " + json.dumps({"type": "response.output_item.done", "item": item}) + "\n\n"
        return {"status": "completed", "output": [item]}
    item = {"type": "tool_use", "id": "call-1", "name": "calculator", "input": {"expression": "1+1"}}
    if stream:
        return "data: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": item}) + "\n\n"
    return {"type": "message", "content": [item]}


@pytest.mark.parametrize("api", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_native_provider_tool_attempt_is_rejected_under_the_same_real_transport(api, stream):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        value = _provider_payload(api, stream)
        return httpx.Response(200, text=value) if stream else httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        gateway = ModelGatewayClient(
            make_settings(model_gateway_url="https://proxy.example.test/openai"),
            http_client=http,
        )
        params = {"tools": [{"type": "function", "function": {
            "name": "calculator", "parameters": {"type": "object"},
        }}]}
        with constrain_request(tools=False, automatic_memory=False):
            with pytest.raises(ModelGatewayError, match="tool-free"):
                if stream:
                    _ = [chunk async for chunk in gateway.stream(
                        deployment="test-deployment", messages=[], params=params, api=api,
                    )]
                else:
                    await gateway.complete(
                        deployment="test-deployment", messages=[], params=params, api=api,
                    )
        assert len(sent) == 1
        assert not sent[0].get("tools")
        # Identical provider payload with the reduction off actually returns the
        # tool event to the governed runtime; a never-entered path cannot pass.
        with constrain_request(tools=True, automatic_memory=True):
            if stream:
                chunks = [chunk async for chunk in gateway.stream(
                    deployment="test-deployment", messages=[], params=params, api=api,
                )]
                assert any(chunk.raw or chunk.response_output_items for chunk in chunks)
            else:
                result = await gateway.complete(
                    deployment="test-deployment", messages=[], params=params, api=api,
                )
                assert result["choices"][0]["message"]["tool_calls"]
        assert len(sent) == 2 and sent[1]["tools"]
