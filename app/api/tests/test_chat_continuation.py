import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import httpx

from ai4ia_api.sessions.chat_continuation import (
    ChatContinuation, ChatContinuationError, ChatContinuationPayload, exact_chat_arguments,
)
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Message, MessageRole, Session
from ai4ia_api.sessions.repository import SessionNotFoundError
from tests.cosmos_deletion_fake import CosmosState
from tests.test_tool_approval_gate import (
    _InjectedModelGateway, _bootstrap, _client, _connector, _hold, _turn,
)
from tests.test_agent_runtime import _assistant_text, _assistant_tool_call
from tests.conftest import make_settings
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec
from ai4ia_api.agents.tool_exec import ToolDefinition, build_tools
from ai4ia_api.agents.tools import ToolRisk, ToolSpec


def resume(client, session, prompt, **overrides):
    return client.post("/api/chat/continue", json={
        "sessionId": session, "messageId": prompt["messageId"], "requestId": prompt["id"],
        "grant": prompt["grant"], "action": "approve", **overrides,
    })


def events(response):
    return [
        json.loads(line[6:]) for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def test_exact_call_runs_before_any_new_model_request_in_original_legacy_turn():
    connector = _connector()
    client = _client(connector)
    try:
        session = _bootstrap(client)
        original = {"to": "owner@example.com", "body": "public hash " + "a" * 64}
        gateway = _InjectedModelGateway(original)
        client.app.state.gateway = gateway
        prompt = _hold(client, session)
        assert gateway.calls == 1 and connector.tool_calls == []
        assert "messageId" in prompt and prompt["operationId"]
        stored = client.get(f"/api/sessions/{session}/messages").json()
        assert len(stored) == 2 and "chatContinuation" not in json.dumps(stored)
        first_id = stored[-1]["id"]
        original_call = connector.call_tool

        async def observed_call(**kwargs):
            assert gateway.calls == 1
            assert kwargs["arguments"] == original
            return await original_call(**kwargs)

        connector.call_tool = observed_call
        gateway.arguments = {"to": "changed@example.com", "body": "must not replace the held call"}
        response = resume(client, session, prompt)
        assert response.status_code == 200 and "[DONE]" in response.text, response.text
        assert len(connector.tool_calls) == 1 and gateway.calls == 2
        final = client.get(f"/api/sessions/{session}/messages").json()
        assert len(final) == 2 and final[-1]["id"] == first_id
        assert final[-1]["continuationStatus"] == "complete"
        assert {step["operationId"] for step in final[-1]["steps"]} == {prompt["operationId"]}
        receipt = final[-1]["executionReceipt"]
        assert receipt["toolCalls"][0]["operationId"] == prompt["operationId"]
        assert receipt["toolCalls"][0]["callId"] == "c1"
        assert "chatContinuation" not in json.dumps(final)
        assert client.app.state.session_repo._sessions[session].deletionProtocol is None
        row = client.app.state.session_repo._messages[session][-1]
        assert row.chatContinuation.payload is None
    finally:
        client.__exit__(None, None, None)


def test_sibling_calls_each_require_their_own_operation_approval_without_model_reconstruction():
    connector = _connector()
    client = _client(connector)
    try:
        session = _bootstrap(client)
        gateway = _InjectedModelGateway(repeat=2)
        client.app.state.gateway = gateway
        first = _hold(client, session)
        response = resume(client, session, first)
        assert response.status_code == 200, response.text
        second = next(item["approvals"][0] for item in events(response) if "approvals" in item)
        assert gateway.calls == 1 and len(connector.tool_calls) == 1
        assert first["operationId"] != second["operationId"]
        assert resume(client, session, first).status_code == 409
        assert resume(client, session, second).status_code == 200
        assert gateway.calls == 2 and len(connector.tool_calls) == 2
        assert len(client.get(f"/api/sessions/{session}/messages").json()) == 2
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("change", ["grant", "request", "new_turn", "settings", "deny", "expiry"])
def test_changed_or_closed_approval_refuses_without_tool_egress(change):
    connector = _connector()
    client = _client(connector)
    try:
        session = _bootstrap(client)
        gateway = _InjectedModelGateway()
        client.app.state.gateway = gateway
        prompt = _hold(client, session)
        changes = {}
        if change == "grant":
            changes["grant"] = "invalid"
        elif change == "request":
            changes["requestId"] = "not-the-request"
        elif change == "new_turn":
            assert _turn(client, session, content="A different request").status_code == 200
        elif change == "settings":
            assert client.patch(f"/api/sessions/{session}", json={"systemPrompt": "Changed"}).status_code == 200
        elif change == "deny":
            assert resume(client, session, prompt, action="deny").status_code == 200
        else:
            row = client.app.state.session_repo._messages[session][-1]
            row.chatContinuation.expiresAt = datetime.now(timezone.utc) - timedelta(seconds=1)
        before = gateway.calls
        result = resume(client, session, prompt, **changes)
        assert result.status_code == 409, result.text
        assert gateway.calls == before and connector.tool_calls == []
    finally:
        client.__exit__(None, None, None)


def test_review_after_reload_rotates_only_the_same_operation_challenge():
    connector = _connector()
    client = _client(connector)
    try:
        session = _bootstrap(client)
        gateway = _InjectedModelGateway()
        client.app.state.gateway = gateway
        first = _hold(client, session)
        review = resume(client, session, first, action="review")
        assert review.status_code == 200
        second = review.json()
        assert first["grant"] != second["grant"]
        for name in ("operationId", "messageId", "argumentsDigest", "expiresAt"):
            assert first[name] == second[name]
        assert resume(client, session, first).status_code == 409
        assert resume(client, session, second).status_code == 200
        assert len(connector.tool_calls) == 1
    finally:
        client.__exit__(None, None, None)


def test_linked_agent_resumes_its_held_call_without_replaying_parent_or_child_models():
    client = _client(_connector())
    sent = []
    responses = [
        _assistant_tool_call("parent-call", "delegate_to_agent", '{"agent":"child","task":"Deliver synthetic data."}'),
        _assistant_tool_call("child-call", "deliver", '{"text":"Synthetic"}'),
        _assistant_text("Child completed."),
        _assistant_text("Parent completed."),
    ]
    requests = []

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        reply = responses[len(requests) - 1]
        message = reply["choices"][0]["message"]
        if "input" in body:
            output = [
                {"type": "function_call", "id": f"fc-{len(requests)}", "call_id": call["id"], **call["function"]}
                for call in message.get("tool_calls", [])
            ]
            if message.get("content"):
                output.append({
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": message["content"]}],
                })
            reply = {
                "id": f"response-{len(requests)}", "status": "completed", "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }
        else:
            reply["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return httpx.Response(200, json=reply)

    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    gateway = ModelGatewayClient(make_settings(model_gateway_url="https://gateway.test/openai"), http_client=http)

    async def deliver(arguments, context):
        assert len(requests) == 2
        sent.append(arguments)
        return {"delivered": True}

    try:
        client.app.state.agents = AgentCatalog(agents=[
            AgentSpec(name="parent", displayName="Parent", description="Parent", systemPrompt="Delegate.", links=["child"]),
            AgentSpec(name="child", displayName="Child", description="Child", systemPrompt="Deliver.", tools=["deliver"]),
        ])
        registry, executor = build_tools([ToolDefinition(
            ToolSpec(name="deliver", description="Deliver", risk=ToolRisk.external),
            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            deliver,
        )])
        client.app.state.tool_registry, client.app.state.tool_executor = registry, executor
        client.app.state.gateway = gateway
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()["id"]
        held = client.post("/api/chat", json={
            "sessionId": session, "content": "@parent Deliver.", "stream": False,
        })
        assert held.status_code == 200, held.text
        prompt = held.json()["approvals"][0]
        assert len(requests) == 2 and sent == []
        response = resume(client, session, prompt)
        assert response.status_code == 200 and "[DONE]" in response.text, response.text
        assert sent == [{"text": "Synthetic"}] and len(requests) == 4
        messages = client.get(f"/api/sessions/{session}/messages").json()
        assert len(messages) == 2 and messages[-1]["content"] == "Parent completed."
        child = messages[-1]["executionReceipt"]["delegations"][0]
        assert child["toolCalls"][0]["operationId"] == prompt["operationId"]
        assert child["toolCalls"][0]["approval"] == "invocation"
        assert child["runtime"]["modelCallCount"] == 2
    finally:
        client.__exit__(None, None, None)
        asyncio.run(http.aclose())


def test_lost_external_acknowledgement_is_unknown_and_never_replayed():
    connector = _connector()
    original = connector.call_tool

    async def lost_ack(**kwargs):
        await original(**kwargs)
        raise ConnectionError("synthetic lost acknowledgement")

    connector.call_tool = lost_ack
    client = _client(connector)
    try:
        session = _bootstrap(client)
        gateway = _InjectedModelGateway()
        client.app.state.gateway = gateway
        prompt = _hold(client, session)
        response = resume(client, session, prompt)
        assert '"error"' in response.text and "[DONE]" not in response.text
        assert len(connector.tool_calls) == 1 and gateway.calls == 1
        assert resume(client, session, prompt).status_code == 409
        assert resume(client, session, prompt, action="review").status_code == 409
        row = client.app.state.session_repo._messages[session][-1]
        assert row.continuationStatus == "unknown" and row.chatContinuation.payload is None
        assert len(connector.tool_calls) == 1
    finally:
        client.__exit__(None, None, None)


def test_old_record_is_refused_without_consuming_grant_or_reconstructing_arguments():
    client = _client(_connector())
    try:
        session = _bootstrap(client)
        gateway = _InjectedModelGateway()
        client.app.state.gateway = gateway
        prompt = _hold(client, session)
        row = client.app.state.session_repo._messages[session][-1]
        row.chatContinuation = None
        response = resume(client, session, prompt)
        assert response.status_code == 409 and "older approval" in response.text
        assert row.pendingApprovals[0].consumed is False and gateway.calls == 1
    finally:
        client.__exit__(None, None, None)


@pytest.fixture(params=["memory", "cosmos-legacy", "cosmos-v1"])
def continuation_repo(request):
    if request.param == "memory":
        return InMemorySessionRepository()
    result = CosmosState().repo()
    result._deletion_enabled = request.param == "cosmos-v1"
    return result


async def seed(repo):
    session = await repo.create_session(Session(id="session", userId="owner"))
    user = Message(id="user", userId="owner", sessionId=session.id, role=MessageRole.user, content="Synthetic")
    await repo.add_message("owner", user)
    message = Message(
        id="assistant", userId="owner", sessionId=session.id, role=MessageRole.assistant,
        chatContinuation=ChatContinuation(
            turnId=user.id, revision=0, status="running",
            expiresAt=datetime.now(timezone.utc) + timedelta(minutes=10),
            payload=ChatContinuationPayload(request={}, seed={"private": "not a public receipt"}),
        ),
    )
    assert await repo.save_chat_assistant("owner", message, expected=None, turn_id=user.id)
    return session, message


async def test_actual_repository_cas_has_one_winner_and_never_returns_private_payload(continuation_repo):
    repo = continuation_repo
    session, message = await seed(repo)
    updated = message.model_copy(deep=True)
    updated.chatContinuation.revision += 1
    updated.chatContinuation.status = "dispatched"
    if hasattr(repo._messages, "before_batch"):
        gate = asyncio.Event()
        arrived = 0

        async def both_read_before_writing(operations, partition):
            nonlocal arrived
            arrived += 1
            if arrived >= 2:
                gate.set()
            await gate.wait()

        repo._messages.before_batch = both_read_before_writing
    outcomes = await asyncio.gather(*[
        repo.save_chat_assistant("owner", updated, expected=message, turn_id="user") for _ in range(2)
    ])
    assert sorted(outcomes) == [False, True]
    with pytest.raises(SessionNotFoundError):
        await repo.read_chat_assistant("other", session.id, message.id)
    public = [row.model_dump(mode="json") for row in await repo.list_messages("owner", session.id)]
    assert "chatContinuation" not in json.dumps(public)
    assert "not a public receipt" not in json.dumps(public)


async def test_checkpoint_size_limit_has_a_real_allowed_control(continuation_repo):
    repo = continuation_repo
    _, message = await seed(repo)
    allowed = message.model_copy(deep=True)
    allowed.chatContinuation.revision += 1
    allowed.chatContinuation.payload.seed["bounded"] = "\u00e9" * 10_000
    assert await repo.save_chat_assistant("owner", allowed, expected=message, turn_id="user")
    too_large = allowed.model_copy(deep=True)
    too_large.chatContinuation.revision += 1
    too_large.chatContinuation.payload.seed["bounded"] = "\u00e9" * 100_000
    with pytest.raises(ChatContinuationError, match="too large"):
        await repo.save_chat_assistant("owner", too_large, expected=allowed, turn_id="user")
    assert await repo.read_chat_assistant("owner", "session", "assistant") == allowed


async def test_terminal_payload_and_authority_cannot_be_restored(continuation_repo):
    repo = continuation_repo
    _, original = await seed(repo)
    closed = original.model_copy(deep=True)
    closed.chatContinuation = original.chatContinuation.cleared("cancelled", "test")
    assert await repo.save_chat_assistant("owner", closed, expected=original, turn_id="user")
    restored = original.model_copy(deep=True)
    restored.chatContinuation.revision = closed.chatContinuation.revision + 1
    with pytest.raises(ChatContinuationError, match="cannot restore"):
        await repo.save_chat_assistant("owner", restored, expected=closed, turn_id="user")


@pytest.mark.parametrize("action", ["new_turn", "clear", "delete"])
async def test_stale_work_cannot_recreate_a_cancelled_or_deleted_pending_call(continuation_repo, action):
    repo = continuation_repo
    session, message = await seed(repo)
    if action == "new_turn":
        await repo.add_message("owner", Message(
            id="next", userId="owner", sessionId=session.id, role=MessageRole.user, content="New task",
        ))
    elif action == "clear":
        await repo.clear_messages("owner", session.id)
    elif session.deletionProtocol == 1:
        await repo.begin_deletion("owner", session.id)
    else:
        await repo.delete_session("owner", session.id)
    updated = message.model_copy(deep=True)
    updated.chatContinuation.revision += 1
    updated.chatContinuation.status = "dispatched"
    if action == "delete":
        with pytest.raises(SessionNotFoundError):
            await repo.save_chat_assistant("owner", updated, expected=message, turn_id="user")
    else:
        assert not await repo.save_chat_assistant("owner", updated, expected=message, turn_id="user")
        assert not await repo.save_chat_assistant("owner", message, expected=None, turn_id="user")


def test_private_execution_arguments_preserve_benign_ids_but_not_explicit_authentication():
    arguments = {"query": "public hash " + "a" * 64, "document_id": "b" * 32}
    assert exact_chat_arguments(json.dumps(arguments)) == arguments
    for value in (
        {"authorization": "Bearer synthetic"}, {"api_key": "synthetic"},
        {"url": "https://example.com/blob?sig=synthetic"},
    ):
        with pytest.raises(ChatContinuationError, match="authentication"):
            exact_chat_arguments(json.dumps(value))
    for raw in ('{"x":1,"x":2}', '{"x":NaN}', "[]", '{"x":"' + "a" * 65536 + '"}'):
        with pytest.raises(ChatContinuationError, match="invalid|limit"):
            exact_chat_arguments(raw)
