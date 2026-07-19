from __future__ import annotations

import asyncio
import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from azure.cosmos.exceptions import CosmosBatchOperationError

from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.routers.chat import (
    ChatRequest,
    _client_request_fingerprint,
    _turn_message,
)
from ai4ia_api.sessions.cosmos_repo import CosmosSessionRepository
from ai4ia_api.sessions.models import Message, MessageRole, MessageStatus, Session


TURN_ID = "123e4567-e89b-42d3-a456-426614174000"
OTHER_TURN_ID = "123e4567-e89b-42d3-b456-426614174001"


def _session(client, *, user: str = "dev-user") -> str:
    response = client.post(
        "/api/sessions",
        json={"title": "Chat", "model": "gpt-5.2"},
        headers={"X-Dev-User": user},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _chat(
    client,
    session_id: str,
    *,
    content: str = "hello",
    headers: dict[str, str] | None = None,
    **changes,
):
    payload = {
        "sessionId": session_id,
        "content": content,
        "stream": False,
        "clientTurnId": TURN_ID,
        **changes,
    }
    return client.post("/api/chat", json=payload, headers=headers)


def test_client_turn_id_is_strictly_validated(client):
    session_id = _session(client)
    for invalid in ("short", TURN_ID.upper(), TURN_ID[:-1] + "g"):
        response = _chat(client, session_id, clientTurnId=invalid)
        assert response.status_code == 422
    assert client.get(f"/api/sessions/{session_id}/messages").json() == []


def test_retry_is_idempotent_and_both_rows_carry_turn_id(client):
    session_id = _session(client)
    first = _chat(client, session_id)
    second = _chat(client, session_id)

    assert first.status_code == second.status_code == 200
    assert first.json()["message"]["id"] == second.json()["message"]["id"]
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert len(messages) == 2
    assert {message["role"] for message in messages} == {"user", "assistant"}
    assert {message["clientTurnId"] for message in messages} == {TURN_ID}
    assert all("clientRequestFingerprint" not in message for message in messages)


def test_incompatible_turn_id_reuse_fails_without_duplicate(client):
    session_id = _session(client)
    assert _chat(client, session_id, content="first").status_code == 200
    conflict = _chat(client, session_id, content="different")
    assert conflict.status_code == 409
    assert "different chat request" in conflict.json()["detail"]
    assert len(client.get(f"/api/sessions/{session_id}/messages").json()) == 2


def test_turn_id_is_scoped_to_owned_target_session(client):
    owned = _session(client)
    foreign = _chat(
        client,
        owned,
        headers={"X-Dev-User": "mallory"},
    )
    assert foreign.status_code == 404

    other = _session(client)
    assert _chat(client, owned).status_code == 200
    assert _chat(client, other).status_code == 200
    owned_ids = {
        message["id"]
        for message in client.get(f"/api/sessions/{owned}/messages").json()
    }
    other_ids = {
        message["id"]
        for message in client.get(f"/api/sessions/{other}/messages").json()
    }
    assert owned_ids.isdisjoint(other_ids)


def test_stream_retry_replays_terminal_turn_without_duplicate(client):
    session_id = _session(client)
    first = _chat(client, session_id, stream=True)
    retry = _chat(client, session_id, stream=True)
    assert first.status_code == retry.status_code == 200
    assert f'"clientTurnId": "{TURN_ID}"' in retry.text
    assert "hello world" in retry.text
    assert len(client.get(f"/api/sessions/{session_id}/messages").json()) == 2


def test_concurrent_same_id_request_reports_in_progress_without_done(client):
    class BlockingGateway:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = 0

        async def complete(self, **_kwargs):
            self.calls += 1
            self.started.set()
            await asyncio.to_thread(self.release.wait, 3)
            return {"choices": [{"message": {"content": "finished"}}]}

    gateway = BlockingGateway()
    client.app.state.gateway = gateway
    session_id = _session(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_chat, client, session_id)
        assert gateway.started.wait(2)
        concurrent = _chat(client, session_id, stream=True)
        assert concurrent.status_code == 200
        assert '"inProgress": true' in concurrent.text
        assert "[DONE]" not in concurrent.text
        gateway.release.set()
        completed = first.result(timeout=3)
    assert completed.status_code == 200
    assert gateway.calls == 1
    assert len(client.get(f"/api/sessions/{session_id}/messages").json()) == 2


def test_stale_streaming_claim_recovers_to_terminal_error(client, monkeypatch):
    chat_module = importlib.import_module("ai4ia_api.routers.chat")
    monkeypatch.setattr(chat_module, "CHAT_TURN_REPLAY_WAIT_SECONDS", 0)
    session_id = _session(client)
    internal_user_id = client.get(f"/api/sessions/{session_id}").json()["userId"]
    body = ChatRequest(
        sessionId=session_id,
        content="hello",
        stream=False,
        clientTurnId=TURN_ID,
    )
    fingerprint = _client_request_fingerprint(body)
    stale = datetime.now(timezone.utc) - timedelta(
        seconds=chat_module.CHAT_TURN_LEASE_SECONDS + 1
    )
    user_message = _turn_message(
        body=body,
        user_id=internal_user_id,
        role=MessageRole.user,
        content="hello",
        fingerprint=fingerprint,
        status=MessageStatus.complete,
        createdAt=stale,
    )
    assistant = _turn_message(
        body=body,
        user_id=internal_user_id,
        role=MessageRole.assistant,
        content="",
        fingerprint=fingerprint,
        status=MessageStatus.streaming,
        createdAt=stale,
    )
    asyncio.run(
        client.app.state.session_repo.claim_chat_turn(
            internal_user_id, user_message, assistant
        )
    )

    recovered = _chat(client, session_id)
    assert recovered.status_code == 200
    assert recovered.json()["message"]["status"] == "error"
    assert "couldn't be completed" in recovered.json()["message"]["content"]


def test_summarize_retry_is_idempotent_and_conflict_safe(client):
    session_id = _session(client)
    first = _chat(client, session_id, content="/summarize")
    retry = _chat(client, session_id, content="/summarize")
    assert first.status_code == retry.status_code == 200
    assert first.json()["message"]["id"] == retry.json()["message"]["id"]
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert len(messages) == 2
    assert [message["clientTurnId"] for message in messages] == [TURN_ID, TURN_ID]
    conflict = _chat(client, session_id, content="/help")
    assert conflict.status_code == 409
    assert len(client.get(f"/api/sessions/{session_id}/messages").json()) == 2


def test_summarize_claim_enforces_session_ownership(client):
    session_id = _session(client, user="owner")
    response = _chat(
        client,
        session_id,
        content="/summarize",
        headers={"X-Dev-User": "mallory"},
    )
    assert response.status_code == 404
    assert (
        client.get(
            f"/api/sessions/{session_id}/messages",
            headers={"X-Dev-User": "owner"},
        ).json()
        == []
    )


class _CosmosClaimSessions:
    def __init__(self, session: Session) -> None:
        self.session = session.model_dump(mode="json")

    async def read_item(self, *, item, partition_key):
        return dict(self.session)


class _CosmosClaimMessages:
    def __init__(self, user_message: Message, assistant: Message) -> None:
        self.items = {
            user_message.id: CosmosSessionRepository._to_doc(user_message),
            assistant.id: CosmosSessionRepository._to_doc(assistant),
        }

    async def execute_item_batch(self, *args, **kwargs):
        raise CosmosBatchOperationError(
            status_code=409, message="conflict", headers={}
        )

    async def read_item(self, *, item, partition_key):
        return dict(self.items[item])


async def test_cosmos_claim_conflict_verifies_and_replays_existing_turn():
    session = Session(id="session", userId="user", model="gpt-5.2")
    body = ChatRequest(
        sessionId=session.id,
        content="/summarize",
        stream=False,
        clientTurnId=TURN_ID,
    )
    fingerprint = _client_request_fingerprint(body)
    user_message = _turn_message(
        body=body,
        user_id="user",
        role=MessageRole.user,
        content="/summarize",
        fingerprint=fingerprint,
        status=MessageStatus.complete,
        fromCommand=True,
    )
    assistant = _turn_message(
        body=body,
        user_id="user",
        role=MessageRole.assistant,
        content="summary",
        fingerprint=fingerprint,
        status=MessageStatus.complete,
        fromCommand=True,
    )
    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _CosmosClaimSessions(session)
    repo._messages = _CosmosClaimMessages(user_message, assistant)

    saved_user, saved_assistant, claimed = await repo.claim_chat_turn(
        "user", user_message, assistant
    )
    assert claimed is False
    assert saved_user.id == user_message.id
    assert saved_assistant.content == "summary"


def test_local_reply_and_direct_tool_rows_carry_turn_id(client):
    local_session = _session(client)
    local = _chat(
        client,
        local_session,
        content="/process_document summarize this",
        stream=True,
    )
    assert local.status_code == 200
    assert f'"clientTurnId": "{TURN_ID}"' in local.text
    local_messages = client.get(
        f"/api/sessions/{local_session}/messages"
    ).json()
    assert [message["clientTurnId"] for message in local_messages] == [
        TURN_ID,
        TURN_ID,
    ]

    tool_session = _session(client)
    tool = _chat(
        client,
        tool_session,
        content="/calculator 2 + 2",
        clientTurnId=OTHER_TURN_ID,
    )
    assert tool.status_code == 200
    tool_messages = client.get(f"/api/sessions/{tool_session}/messages").json()
    assert [message["clientTurnId"] for message in tool_messages] == [
        OTHER_TURN_ID,
        OTHER_TURN_ID,
    ]


def test_agent_tool_loop_rows_carry_turn_id(client):
    session_id = _session(client)
    response = _chat(
        client,
        session_id,
        content="/generate_image a red bicycle",
    )
    assert response.status_code == 200
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert [message["clientTurnId"] for message in messages] == [TURN_ID, TURN_ID]


def test_accepted_gateway_error_keeps_correlated_terminal_rows(client):
    class FailingGateway:
        async def complete(self, **_kwargs):
            raise ModelGatewayError(503, "temporarily unavailable")

    client.app.state.gateway = FailingGateway()
    session_id = _session(client)
    response = _chat(client, session_id)
    assert response.status_code == 502
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert [message["clientTurnId"] for message in messages] == [TURN_ID, TURN_ID]
    assert messages[-1]["status"] == "error"


def test_unexpected_plain_completion_error_is_terminal_and_replayable(client):
    class FailingGateway:
        async def complete(self, **_kwargs):
            raise RuntimeError("internal detail must not be persisted")

    client.app.state.gateway = FailingGateway()
    session_id = _session(client)
    with pytest.raises(RuntimeError):
        _chat(client, session_id)
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert messages[-1]["status"] == "error"
    assert "internal detail" not in messages[-1]["content"]
    replay = _chat(client, session_id)
    assert replay.status_code == 200
    assert replay.json()["message"]["status"] == "error"


def test_unexpected_nonstream_agent_error_is_terminal_and_replayable(
    client, monkeypatch
):
    async def fail_agent_turn(**_kwargs):
        raise RuntimeError("tool arguments must not be persisted")

    chat_module = importlib.import_module("ai4ia_api.routers.chat")
    monkeypatch.setattr(chat_module, "run_agent_turn", fail_agent_turn)
    session_id = _session(client)
    with pytest.raises(RuntimeError):
        _chat(client, session_id, content="@general hello")
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert messages[-1]["status"] == "error"
    assert "tool arguments" not in messages[-1]["content"]
    replay = _chat(client, session_id, content="@general hello")
    assert replay.status_code == 200
    assert replay.json()["message"]["status"] == "error"


def test_historical_message_without_turn_id_still_validates():
    message = Message.model_validate(
        {
            "id": "old",
            "sessionId": "session",
            "userId": "user",
            "role": "assistant",
            "content": "legacy",
            "status": "complete",
        }
    )
    assert message.clientTurnId is None
