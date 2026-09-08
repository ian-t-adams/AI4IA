from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.identity import CredentialUnavailableError

from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec
from ai4ia_api.agents.consent_service import execution_tools_for_state
from ai4ia_api.agents.receipt import ReceiptDraft
from ai4ia_api.agents.runtime import AgentRunCancelled, AgentRunFailed, run_agent_turn
from ai4ia_api.agents.tool_exec import ToolContext, build_tools
from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.memory.context import MemoryContextGuard
from ai4ia_api.memory.preferences import MemoryPreference, MemoryPreferenceUnavailable
from ai4ia_api.workflows.models import Workflow, WorkflowStep
from ai4ia_api.workflows.runner import run_workflow, run_workflow_step
from tests.test_memory_preference import cosmos_memory, set_automatic
from tests.test_chat_stream_protocol import _PersistingRepo, _test_agentic_stream
from tests.test_memory_preference_execution import (
    HEADERS,
    OWNER_FACT,
    RecordingGateway,
    execution_client as execution_client,
)


@pytest.mark.parametrize("failure", [
    None,
    CosmosHttpResponseError(status_code=503, message="unavailable"),
    ServiceRequestError("transport unavailable"),
    ServiceResponseError("response interrupted"),
    ClientAuthenticationError("authentication unavailable"),
    CredentialUnavailableError("credential unavailable"),
])
def test_preference_sdk_failures_withhold_memory_without_failing_chat(
    execution_client, monkeypatch, failure
):
    client, session, container = execution_client
    original = container.read_item

    async def read(**kwargs):
        if failure is not None:
            raise failure
        return await original(**kwargs)

    monkeypatch.setattr(container, "read_item", read)
    preference = client.get("/api/memories/preference", headers=HEADERS)
    assert preference.status_code == (503 if failure else 200)
    patched = client.patch(
        "/api/memories/preference",
        headers={**HEADERS, "If-Match": '"memory-preference-0"'},
        json={"automaticMemoryEnabled": True},
    )
    assert patched.status_code == (503 if failure else 200)
    response = client.post("/api/chat", headers=HEADERS, json={
        "sessionId": session["id"], "content": "Describe my preferences", "stream": False,
    })
    assert response.status_code == 200, response.text
    requests = client.app.state.gateway.requests
    assert len(requests) == 1
    assert (OWNER_FACT in json.dumps(requests[0])) is (failure is None)


@pytest.mark.parametrize("method", ["get", "set"])
async def test_preference_sdk_boundary_never_swallows_cancellation(monkeypatch, method):
    memory, _store, _embedder, _planner, container = cosmos_memory()

    async def cancelled(**kwargs):
        await asyncio.sleep(0)
        raise asyncio.CancelledError()

    monkeypatch.setattr(container, "read_item", cancelled)
    with pytest.raises(asyncio.CancelledError):
        if method == "get":
            await memory.get_preference("alice")
        else:
            await memory.set_preference("alice", False, expected_etag='"memory-preference-0"')


@pytest.mark.parametrize("failure", [
    ServiceRequestError("transport unavailable"), ServiceResponseError("response interrupted"),
    ClientAuthenticationError("authentication unavailable"), asyncio.CancelledError(),
])
async def test_preference_write_translates_only_sdk_failures(monkeypatch, failure):
    memory, _store, _embedder, _planner, container = cosmos_memory()
    current = await memory.get_preference("alice")

    async def fail(**kwargs):
        await asyncio.sleep(0)
        raise failure

    monkeypatch.setattr(container, "replace_item", fail)
    expected = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else MemoryPreferenceUnavailable
    with pytest.raises(expected):
        await memory.set_preference("alice", False, expected_etag=current.etag)


@pytest.mark.parametrize("boundary", ["context", "gateway"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_context_await_preserves_completed_work_without_inventing_a_request(boundary, outcome):
    registry, executor = build_tools()
    requests = []
    prepared = []

    async def interrupt():
        await asyncio.sleep(0)
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "failure":
            raise RuntimeError("test boundary failure")

    async def prepare(messages):
        prepared.append(copy.deepcopy(messages))
        if boundary == "context" and len(prepared) == 2:
            await interrupt()

    class Gateway:
        async def complete(self, *, messages, **kwargs):
            requests.append(copy.deepcopy(messages))
            if len(requests) == 1:
                return {
                    "choices": [{"message": {"role": "assistant", "content": "Calculating.",
                        "tool_calls": [{"id": "c1", "type": "function", "function": {
                            "name": "calculator", "arguments": '{"expression":"2+3"}',
                        }}],
                    }}],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
                }
            if boundary == "gateway":
                await interrupt()
            return {
                "choices": [{"message": {"content": "Five."}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    async def build(_names, ctx):
        return registry, executor, replace(ctx, prepare_model_context=prepare)

    result = await run_workflow(
        Workflow(id="calc", userId="alice", name="calc", displayName="Calculate",
                 steps=[WorkflowStep(agent="calc", instruction="{input}")]),
        run_input="Calculate", composed=AgentCatalog(agents=[AgentSpec(
            name="calc", displayName="Calculate", description="Calculate",
            systemPrompt="Use the calculator.", tools=["calculator"],
        )]), deployment="test", gateway=Gateway(), registry=registry, executor=executor,
        tool_builder=build,
    )
    sent = 1 if boundary == "context" and outcome != "success" else 2
    assert len(prepared) == 2
    assert len(requests) == sent
    assert result.ok is (outcome == "success")
    assert result.cancelled is (outcome == "cancel")
    assert result.usage.total == (14 if outcome == "success" else 12)
    assert result.usage.calls == sent
    assert result.steps[0].activity
    receipt = result.steps[0].receipt
    assert receipt is not None
    assert receipt.iterations == sent
    assert receipt.toolCallCount == 1
    assert '"result":5' in receipt.toolCalls[0].result.text
    assert "workflow_step_not_started" not in receipt.notes
    assert len(receipt.modelRequests) == sent - 1
    assert [item.role for item in receipt.prompt] == ["system", "user"]


@pytest.mark.parametrize("cancel", [False, True])
async def test_interrupted_initial_context_has_no_attempted_request(cancel):
    registry, executor = build_tools()
    gateway = RecordingGateway()

    async def prepare(_messages):
        await asyncio.sleep(0)
        if cancel:
            raise asyncio.CancelledError()
        raise RuntimeError("context failure")

    with pytest.raises(AgentRunCancelled if cancel else AgentRunFailed) as caught:
        await run_agent_turn(
            deployment="test", messages=[{"role": "user", "content": "Hello"}],
            tool_names=[], gateway=gateway, registry=registry, executor=executor,
            ctx=ToolContext(prepare_model_context=prepare), retain_failed_request=True,
        )
    partial = caught.value.partial
    assert partial.iterations == partial.usage.calls == 0
    assert partial.model_requests == partial.effective_prompt == gateway.requests == []


@pytest.mark.parametrize("cancel_at", ["preference", "gateway"])
async def test_task_cancellation_at_real_workflow_preference_boundary_retains_work(cancel_at):
    waiting = asyncio.Event()

    class Gateway:
        calls = 0

        async def complete(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [{"message": {"content": "Calculating.", "tool_calls": [
                        {"id": "calc", "type": "function", "function": {
                            "name": "calculator", "arguments": '{"expression":"1+1"}',
                        }},
                    ]}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                }
            waiting.set()
            await asyncio.Future()

    gateway = Gateway()

    class Memory:
        enabled = True

        async def get_preference(self, _user_id):
            if gateway.calls and cancel_at == "preference":
                waiting.set()
                await asyncio.Future()
            return MemoryPreference()

    registry, executor = build_tools()
    state = SimpleNamespace(memory=Memory(), tool_registry=registry, tool_executor=executor)
    task = asyncio.create_task(run_workflow_step(
        WorkflowStep(agent="calc", instruction="{input}"), index=0, workflow_name="calc",
        run_input="Calculate", previous="", composed=AgentCatalog(agents=[AgentSpec(
            name="calc", displayName="Calculate", description="Calculate",
            systemPrompt="Use the calculator.", tools=["calculator"],
        )]), deployment="test", gateway=gateway, registry=registry, executor=executor,
        tool_builder=lambda names, ctx: execution_tools_for_state(
            state, user_id="alice", tool_names=names, ctx=ctx,
        ),
    ))
    await asyncio.wait_for(waiting.wait(), timeout=2)
    task.cancel()
    outcome = await task
    assert outcome.result.cancelled
    assert outcome.usage.total == 12
    assert outcome.usage.calls == (1 if cancel_at == "preference" else 2)
    assert len(outcome.result.activity) == 1
    assert outcome.result.receipt.notes == []
    assert outcome.result.receipt.iterations == gateway.calls


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("disable", [False, True])
@pytest.mark.parametrize("failed_iteration", [1, 2])
def test_failure_receipt_keeps_actual_first_prompt_after_memory_withholding(
    execution_client, monkeypatch, stream, disable, failed_iteration
):
    client, session, _container = execution_client
    memory = client.app.state.memory

    class Gateway(RecordingGateway):
        async def complete(self, **kwargs):
            result = await super().complete(**kwargs)
            if len(self.requests) == failed_iteration:
                raise ModelGatewayError(502, "offline gateway failure")
            if disable:
                await set_automatic(memory, session["userId"], False)
            return result

    gateway = Gateway(["recall_memory"] if failed_iteration == 2 else [])
    client.app.state.gateway = gateway
    assert client.post("/api/agents", headers=HEADERS, json={
        "name": "memory-tester", "systemPrompt": "Answer.", "tools": ["recall_memory"],
    }).status_code == 201
    if failed_iteration == 1 and disable:
        original = client.app.state.session_repo.patch_session

        async def disable_after_context(*args, **kwargs):
            result = await original(*args, **kwargs)
            await set_automatic(memory, session["userId"], False)
            return result

        monkeypatch.setattr(client.app.state.session_repo, "patch_session", disable_after_context)
    response = client.post("/api/chat", headers=HEADERS, json={
        "sessionId": session["id"], "content": "@memory-tester my preferences", "stream": stream,
    })
    assert response.status_code == (200 if stream else 502), response.text
    assert len(gateway.requests) == failed_iteration
    first_supplied = not disable or failed_iteration == 2
    assert (OWNER_FACT in json.dumps(gateway.requests[0])) is first_supplied
    assert (OWNER_FACT in json.dumps(gateway.requests[-1])) is (not disable)
    messages = client.get(f"/api/sessions/{session['id']}/messages", headers=HEADERS).json()
    receipt = messages[-1]["executionReceipt"]
    assert messages[-1]["status"] == "error"
    assert (OWNER_FACT in json.dumps(receipt["prompt"])) is first_supplied
    block = next(block for block in receipt["contextBlocks"] if block["kind"] == "memory")
    assert block["admitted"] is first_supplied
    if failed_iteration == 2:
        assert (OWNER_FACT in json.dumps(receipt["modelRequests"][-1])) is (not disable)


@pytest.mark.parametrize("iteration", [1, 2])
@pytest.mark.parametrize("disable", [False, True])
async def test_stream_cancellation_retains_actual_first_prompt_and_completed_evidence(iteration, disable):
    memory, _store, _embedder, _planner, _container = cosmos_memory()
    guard = MemoryContextGuard(memory, "user")
    assert await guard.allowed()
    guard.block = OWNER_FACT
    messages = [
        {"role": "system", "content": OWNER_FACT},
        {"role": "user", "content": "Recall preferences"},
    ]
    draft = ReceiptDraft(prompt_messages=copy.deepcopy(messages))
    guard.on_withheld = lambda filtered: setattr(draft, "prompt_messages", copy.deepcopy(filtered))
    if disable and iteration == 1:
        await set_automatic(memory, "user", False)
    requests = []
    waiting = asyncio.Event()

    class Gateway:
        async def complete(self, *, messages, **kwargs):
            requests.append(copy.deepcopy(messages))
            if len(requests) == iteration:
                waiting.set()
                await asyncio.Future()
            if disable:
                await set_automatic(memory, "user", False)
            return {
                "choices": [{"message": {"content": "", "tool_calls": [
                    {"id": "c1", "function": {"name": "calculator", "arguments": '{"expression":"2+3"}'}},
                ]}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            }

    registry, executor = build_tools()

    async def run(on_step):
        return await run_agent_turn(
            deployment="test", messages=messages, tool_names=["calculator"],
            gateway=Gateway(), registry=registry, executor=executor,
            ctx=ToolContext(prepare_model_context=guard.prepare), on_step=on_step,
        )

    repo = _PersistingRepo()
    stream = _test_agentic_stream(run=run, repo=repo, receipt_draft=draft)
    await anext(stream)
    await asyncio.wait_for(waiting.wait(), timeout=2)
    await stream.aclose()
    saved = repo.persisted[-1]
    assert saved.status == "cancelled"
    receipt = saved.executionReceipt
    assert receipt is not None
    assert receipt.iterations == iteration
    assert receipt.usage.calls == iteration
    assert receipt.usage.totalTokens == (12 if iteration == 2 else None)
    assert receipt.toolCallCount == (1 if iteration == 2 else 0)
    first_supplied = not disable or iteration == 2
    assert (OWNER_FACT in json.dumps(requests[0])) is first_supplied
    assert (OWNER_FACT in json.dumps(receipt.model_dump()["prompt"])) is first_supplied
    if iteration == 2:
        assert (OWNER_FACT in json.dumps(receipt.model_dump()["modelRequests"][-1])) is (not disable)


@pytest.mark.parametrize("surface", ["plain", "agent", "fallback"])
@pytest.mark.parametrize("transition", ["on", "off", "off_on"])
@pytest.mark.parametrize("stream", [False, True])
def test_memory_fence_and_gateway_evidence_share_the_actual_request(
    monkeypatch, surface, transition, stream
):
    from tests.test_memory_preference_execution import AvailableWebSearch
    from tests.test_model_call_evidence import receipts, transport_client

    def respond(_body, index, result):
        if surface == "fallback" and index == 1:
            result["choices"] = [{"message": {"content": ""}}]
        return result

    with transport_client(respond=respond) as (client, requests):
        memory, _store, _embedder, _planner, _container = cosmos_memory()
        client.app.state.memory = memory
        assert client.post("/api/memories", json={"text": OWNER_FACT}).status_code == 201
        created = client.post("/api/sessions", json={"title": "Merge seam", "model": "gpt-5.2"})
        assert created.status_code == 201
        session = created.json()
        content = "Describe my preferences"
        if surface == "agent":
            assert client.post("/api/agents", json={
                "name": "memory-tester", "systemPrompt": "Answer accurately.", "tools": ["calculator"],
            }).status_code == 201
            content = "@memory-tester " + content
        elif surface == "fallback":
            client.app.state.web_search = AvailableWebSearch()
        original = client.app.state.session_repo.patch_session
        entered = []

        async def after_context(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.append(True)
            if transition != "on":
                await set_automatic(memory, session["userId"], False)
                if transition == "off_on":
                    await set_automatic(memory, session["userId"], True)
            return result

        monkeypatch.setattr(client.app.state.session_repo, "patch_session", after_context)
        response = client.post("/api/chat", json={
            "sessionId": session["id"], "content": content, "stream": stream,
            "params": {"max_tokens": 2048, "reasoning_effort": "low"},
        })
        assert response.status_code == 200, response.text
        assert entered
        saved = receipts(client, session["id"])[0]
        expected_calls = 2 if surface == "fallback" else 1
        assert len(requests) == saved["runtime"]["modelCallCount"] == expected_calls
        block = next(item for item in saved["contextBlocks"] if item["kind"] == "memory")
        assert block["admitted"] is (transition == "on")
        assert (OWNER_FACT in json.dumps(saved["prompt"])) is (transition == "on")
        for wire, call in zip(requests, saved["runtime"]["modelCalls"], strict=True):
            assert (OWNER_FACT in json.dumps(wire["messages"])) is (transition == "on")
            assert call["coverage"] == "recorded" and call["httpAttempts"] == 1
            assert call["parameters"]["maxOutputTokens"] == wire["max_completion_tokens"] == 2048
            assert call["parameters"]["reasoningEffort"] == wire["reasoning_effort"] == "low"
            assert call["cost"]["priceVersion"] == "receipt-test-v1"
            assert call["cost"]["estCostMicroUsd"] == 4000
        assert saved["usage"]["cost"]["totalCalls"] == expected_calls
        assert saved["usage"]["cost"]["estCostMicroUsd"] == 4000 * expected_calls
        if surface == "fallback":
            assert requests[0]["tools"]
            assert "tools" not in requests[1]
