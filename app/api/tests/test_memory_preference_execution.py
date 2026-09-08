from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from azure.cosmos.exceptions import CosmosHttpResponseError
from fastapi.testclient import TestClient

from ai4ia_api.agents.capabilities import build_shared_capabilities
from ai4ia_api.agents.runtime import run_agent_turn
from ai4ia_api.agents.tool_exec import ToolContext, build_tools
from ai4ia_api.main import create_app
from ai4ia_api.memory.context import MemoryContextGuard
from ai4ia_api.memory.recall_capability import build_recall_capability
from ai4ia_api.memory.remember_capability import build_remember_capability
from ai4ia_api.workflows.durable import DurableWorkflowService
from ai4ia_api.workflows.models import WorkflowStep
from tests.conftest import make_settings, sse_chunks
from tests.test_memory_preference import cosmos_memory, set_automatic

OWNER_FACT = "The owner prefers concise answers"
MODEL_FACT = "The owner prefers examples in Python"
HEADERS = {"X-Dev-User": "alice"}


class RecordingGateway:
    def __init__(self, tools=()):
        self.requests = []
        self.offered = []
        self.tools = tools
        self.after_request = None

    async def complete(self, *, messages, params=None, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        self.offered.append(copy.deepcopy((params or {}).get("tools") or []))
        if len(self.requests) == 1 and self.after_request is not None:
            await self.after_request()
        calls = [
            {
                "id": name, "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(
                        {"query": "preferences"} if name == "recall_memory" else {"text": MODEL_FACT}
                    ),
                },
            }
            for name in self.tools
        ] if len(self.requests) == 1 else []
        message = {"role": "assistant", "content": "done"}
        if calls:
            message["tool_calls"] = calls
        return {
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    async def stream(self, **kwargs):
        response = await self.complete(**kwargs)
        for chunk in sse_chunks(response):
            yield chunk


class AvailableWebSearch:
    def build_capability(self, **kwargs):
        async def handler(args, ctx):
            return {"results": []}

        return [
            {"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}
        ], {"web_search": handler}


@pytest.fixture
def execution_client():
    # Isolate the capability switch from approval policy; the positive control
    # must actually execute the write, not stop at an unrelated consent gate.
    app = create_app(make_settings(tool_approval_mode="off"))
    with TestClient(app) as client:
        memory, _store, _embedder, _planner, container = cosmos_memory()
        app.state.memory = memory
        app.state.gateway = RecordingGateway()
        created = client.post("/api/memories", headers=HEADERS, json={"text": OWNER_FACT})
        assert created.status_code == 201
        session = client.post(
            "/api/sessions", headers=HEADERS, json={"title": "Chat", "model": "gpt-5.2"}
        ).json()
        yield client, session, container


@pytest.mark.parametrize("surface", ["plain", "agent", "plain_tool"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("preference", ["on", "off", "off_on", "unknown"])
def test_late_preference_check_controls_actual_prompt_and_receipt(
    execution_client, monkeypatch, surface, stream, preference
):
    client, session, container = execution_client
    content = "Which answer style do I prefer?"
    if surface == "agent":
        created = client.post(
            "/api/agents", headers=HEADERS,
            json={"name": "memory-tester", "systemPrompt": "Answer accurately.", "tools": ["calculator"]},
        )
        assert created.status_code == 201
        content = "@memory-tester " + content
    elif surface == "plain_tool":
        client.app.state.web_search = AvailableWebSearch()
    memory = client.app.state.memory
    original = client.app.state.session_repo.patch_session
    entered = []

    async def after_context(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.append(True)
        if preference in {"off", "off_on"}:
            await set_automatic(memory, session["userId"], False)
            if preference == "off_on":
                await set_automatic(memory, session["userId"], True)
        elif preference == "unknown":
            async def unavailable(**options):
                raise CosmosHttpResponseError(status_code=503, message="state unavailable")
            monkeypatch.setattr(container, "read_item", unavailable)
        return result

    monkeypatch.setattr(client.app.state.session_repo, "patch_session", after_context)
    response = client.post(
        "/api/chat", headers=HEADERS,
        json={"sessionId": session["id"], "content": content, "stream": stream},
    )
    assert response.status_code == 200, response.text
    assert entered
    gateway = client.app.state.gateway
    assert len(gateway.requests) == 1
    system = [message["content"] for message in gateway.requests[0] if message["role"] == "system"]
    assert (OWNER_FACT in "\n".join(system)) is (preference == "on")
    if surface != "plain":
        assert gateway.offered[0]
    messages = client.get(f"/api/sessions/{session['id']}/messages", headers=HEADERS).json()
    receipt = messages[-1]["executionReceipt"]
    block = next(item for item in receipt["contextBlocks"] if item["kind"] == "memory")
    assert block["admitted"] is (preference == "on")
    assert any(OWNER_FACT in item["content"]["text"] for item in receipt["prompt"]) is (preference == "on")


@pytest.mark.parametrize("surface", ["plain", "agent"])
@pytest.mark.parametrize("disable", [False, True])
def test_stream_rechecks_after_placeholder_persistence(execution_client, monkeypatch, surface, disable):
    client, session, _container = execution_client
    if surface == "agent":
        assert client.post("/api/agents", headers=HEADERS, json={
            "name": "memory-tester", "systemPrompt": "Answer.", "tools": ["calculator"],
        }).status_code == 201
    repo = client.app.state.session_repo
    original = repo.add_message
    entered = []

    async def add_message(uid, message):
        result = await original(uid, message)
        if message.role == "assistant" and message.status == "streaming":
            entered.append(True)
            if disable:
                await set_automatic(client.app.state.memory, uid, False)
        return result

    monkeypatch.setattr(repo, "add_message", add_message)
    response = client.post("/api/chat", headers=HEADERS, json={
        "sessionId": session["id"], "stream": True,
        "content": ("@memory-tester " if surface == "agent" else "") + "Describe my preferences",
    })
    assert response.status_code == 200, response.text
    assert entered
    assert (OWNER_FACT in json.dumps(client.app.state.gateway.requests)) is not disable


@pytest.mark.parametrize("surface", [
    "agent", "agent_stream", "recall_command", "remember_command", "workflow",
    "durable", "resumed", "delayed_disabled", "resumed_disabled",
])
@pytest.mark.parametrize("disable", [False, True])
def test_model_tools_recheck_after_model_await_across_execution_surfaces(
    execution_client, surface, disable
):
    client, session, _container = execution_client
    names = (
        ["recall_memory"] if surface == "recall_command" else
        ["remember_memory"] if surface == "remember_command" else
        ["recall_memory", "remember_memory"]
    )
    gateway = RecordingGateway(names)
    client.app.state.gateway = gateway
    memory = client.app.state.memory

    async def after_model():
        if disable:
            await set_automatic(memory, session["userId"], False)

    gateway.after_request = after_model
    created = client.post("/api/agents", headers=HEADERS, json={
        "name": "memory-tester", "systemPrompt": "Use the supplied memory tools.", "tools": names,
    })
    assert created.status_code == 201
    if surface in {"durable", "resumed", "delayed_disabled", "resumed_disabled"}:
        async def activity():
            deployment = client.app.state.catalog.resolve_deployment("gpt-5.2")
            composed = await client.app.state.agent_service.catalog_for(
                session["userId"], client.app.state.agents
            )
            # Frozen before execution, like a delayed or resumed activity. The
            # preference must still be read from current canonical memory state.
            context = json.loads(json.dumps({
                "userId": session["userId"], "sessionId": session["id"],
                "workflowName": "memory-flow", "runInput": "Use my preferences",
                "deployment": deployment.deploymentName, "approvalPolicy": "off",
                "agentSnapshot": composed.model_dump(mode="json"),
            }))
            if disable and surface in {"delayed_disabled", "resumed_disabled"}:
                await set_automatic(memory, session["userId"], False)
            service = DurableWorkflowService(
                endpoint="https://unused.test", task_hub="unused", app_state=client.app.state
            )
            return await service._execute_step(
                step=WorkflowStep(agent="memory-tester", instruction="{input} {previous}"),
                index=1 if surface.startswith("resumed") else 0,
                previous="A previously completed step" if surface.startswith("resumed") else "",
                context=context,
            )

        assert client.portal.call(activity)["result"]["ok"]
    elif surface == "workflow":
        assert client.post("/api/workflows", headers=HEADERS, json={
            "name": "memory-flow",
            "steps": [{"agent": "memory-tester", "instruction": "{input}"}],
        }).status_code == 201
        response = client.post("/api/workflows/memory-flow/run", headers=HEADERS, json={
            "sessionId": session["id"], "input": "Use my preferences",
        })
        assert response.status_code == 200, response.text
        assert response.json()["ok"]
    else:
        content = {
            "recall_command": "/recall_memory my preferences",
            "remember_command": "/remember_memory " + MODEL_FACT,
        }.get(surface, "@memory-tester recall my preferences and save a fact")
        response = client.post("/api/chat", headers=HEADERS, json={
            "sessionId": session["id"], "content": content, "stream": surface == "agent_stream",
        })
        assert response.status_code == 200, response.text

    assert len(gateway.requests) == 2
    results = {
        message["tool_call_id"]: json.loads(message["content"])
        for message in gateway.requests[-1] if message["role"] == "tool"
    }
    if "recall_memory" in names:
        assert (OWNER_FACT in results["recall_memory"]["results"]) is not disable
    if "remember_memory" in names:
        assert results["remember_memory"]["saved"] is not disable
        texts = [item["text"] for item in client.get("/api/memories", headers=HEADERS).json()["items"]]
        assert (MODEL_FACT in texts) is not disable


@pytest.mark.parametrize("disable", [False, True])
async def test_runtime_rechecks_after_recording_a_memory_tool_result(disable):
    memory, _store, _embedder, _planner, _container = cosmos_memory()
    await memory.create_memory("alice", OWNER_FACT)
    gateway = RecordingGateway(["recall_memory"])
    built = build_shared_capabilities(
        attached_tool_names=["recall_memory"], memory=memory, user_id="alice", nonce="test",
    )
    registry, executor = build_tools()
    guard = MemoryContextGuard(memory, "alice")
    entered = []

    async def on_step(step):
        if step.kind == "delegate" and step.tool == "recall_memory":
            entered.append(step.result)
            if disable:
                await set_automatic(memory, "alice", False)

    result = await run_agent_turn(
        deployment="test-model", messages=[{"role": "user", "content": "Recall preferences"}],
        tool_names=[], gateway=gateway, registry=registry, executor=executor,
        ctx=ToolContext(prepare_model_context=guard.prepare), extra_tools=built.tools,
        extra_handlers=built.handlers, on_step=on_step,
    )
    assert entered and OWNER_FACT in entered[0]["results"]
    assert (OWNER_FACT in json.dumps(gateway.requests[-1])) is not disable
    assert (OWNER_FACT in json.dumps(result.model_requests[-1])) is not disable
    assert OWNER_FACT in json.dumps(result.steps[0].result)


@pytest.mark.parametrize("disable", [False, True])
async def test_prebuilt_handlers_do_not_cache_enabled_preference(disable):
    memory, _store, _embedder, _planner, _container = cosmos_memory()
    await memory.create_memory("alice", OWNER_FACT)
    _, recalls = build_recall_capability(memory=memory, user_id="alice", nonce="test")
    _, saves = build_remember_capability(memory=memory, user_id="alice")
    if disable:
        await set_automatic(memory, "alice", False)
    assert bool((await recalls["recall_memory"]({"query": "preferences"}, ToolContext()))["count"]) is not disable
    assert (await saves["remember_memory"]({"text": MODEL_FACT}, ToolContext()))["saved"] is not disable


async def test_context_filter_does_not_erase_historical_messages_or_other_tools():
    memory, _store, _embedder, _planner, _container = cosmos_memory()
    guard = MemoryContextGuard(memory, "alice")
    assert await guard.allowed()
    guard.block = "A current recalled memory block"
    history = [
        {"role": "system", "content": "The owner set these instructions"},
        {"role": "system", "content": guard.block},
        {"role": "assistant", "content": "A historical answer quoting a memory"},
        {"role": "user", "content": "New question"},
        {"role": "assistant", "tool_calls": [
            {"id": "recall", "function": {"name": "recall_memory"}},
            {"id": "other", "function": {"name": "fetch_document"}},
        ]},
        {"role": "tool", "tool_call_id": "recall", "content": "Recalled context"},
        {"role": "tool", "tool_call_id": "other", "content": "Document context"},
    ]
    request = copy.deepcopy(history)
    await set_automatic(memory, "alice", False)
    await guard.prepare(request)
    assert len(request) == len(history) - 1
    assert request[1]["content"] == "A historical answer quoting a memory"
    assert request[-1]["content"] == "Document context"
    assert history[1]["content"] == guard.block and history[-2]["content"] == "Recalled context"


async def test_missing_preference_reader_is_not_treated_as_default_enabled():
    memory = SimpleNamespace(enabled=True)
    assert not await MemoryContextGuard(memory, "alice").allowed()


def test_off_preserves_forget_commands_and_historical_receipts(execution_client):
    client, session, _container = execution_client
    response = client.post("/api/chat", headers=HEADERS, json={
        "sessionId": session["id"], "content": "Describe my preferred answer style", "stream": False,
    })
    assert response.status_code == 200
    path = f"/api/sessions/{session['id']}/messages"
    original_messages = client.get(path, headers=HEADERS).json()
    assert OWNER_FACT in json.dumps(original_messages[-1]["executionReceipt"])
    preference = client.get("/api/memories/preference", headers=HEADERS).json()
    assert client.patch("/api/memories/preference", headers={
        **HEADERS, "If-Match": preference["etag"],
    }, json={"automaticMemoryEnabled": False}).status_code == 200
    bob = {"X-Dev-User": "bob"}
    assert client.post("/api/memories", headers=bob, json={"text": "Bob's retained record"}).status_code == 201
    gateway_calls = len(client.app.state.gateway.requests)
    for command, expected in [("/forget", "Forgot 1"), ("/forget me", "Forgot all 1")]:
        forgotten = client.post("/api/chat", headers=HEADERS, json={
            "sessionId": session["id"], "content": command, "stream": False,
        })
        assert forgotten.status_code == 200
        assert forgotten.json()["message"]["content"].startswith(expected)
    assert len(client.app.state.gateway.requests) == gateway_calls
    assert client.get(path, headers=HEADERS).json()[:2] == original_messages
    assert client.get("/api/memories", headers=HEADERS).json()["items"] == []
    assert client.get("/api/memories", headers=bob).json()["items"][0]["text"] == "Bob's retained record"
    assert client.get("/api/memories/preference", headers=HEADERS).json()["automaticMemoryEnabled"] is False
