from __future__ import annotations

import asyncio
import json

import pytest
from starlette.responses import StreamingResponse

from ai4ia_api.agents.runtime import AgentRunResult
from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.catalog import DeploymentOption
from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.routers import chat as chat_router
from ai4ia_api.routers.chat import _agentic_stream, _stream_with_placeholder
from ai4ia_api.sessions.models import Message, MessageRole, MessageStatus


def _create_session(client) -> str:
    response = client.post(
        "/api/sessions",
        json={"title": "Chat", "model": "gpt-5.2"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _sse_payloads(response_text: str) -> list[str]:
    return [
        line.removeprefix("data: ")
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


def _track_terminal_yields(monkeypatch, events: list[str]) -> None:
    class TrackedStreamingResponse(StreamingResponse):
        def __init__(self, content, *args, **kwargs):
            async def tracked_content():
                async for chunk in content:
                    text = chunk.decode() if isinstance(chunk, bytes) else chunk
                    if "data: [DONE]" in text:
                        events.append("done")
                    elif '"error"' in text:
                        events.append("error")
                    yield chunk

            super().__init__(tracked_content(), *args, **kwargs)

    monkeypatch.setattr(chat_router, "StreamingResponse", TrackedStreamingResponse)


def test_plain_stream_persists_terminal_row_before_done(client, monkeypatch):
    session_id = _create_session(client)
    repo = client.app.state.session_repo
    original_upsert = repo.upsert_message
    events: list[str] = []
    _track_terminal_yields(monkeypatch, events)

    async def tracked_upsert(user_id, message):
        result = await original_upsert(user_id, message)
        events.append(f"upsert:{message.status.value}")
        return result

    monkeypatch.setattr(repo, "upsert_message", tracked_upsert)
    response = client.post(
        "/api/chat",
        json={"sessionId": session_id, "content": "hello", "stream": True},
    )
    payloads = _sse_payloads(response.text)

    metadata = json.loads(payloads[0])["metadata"]
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert metadata == {
        "userMessageId": messages[0]["id"],
        "assistantMessageId": messages[1]["id"],
    }
    assert events.index("upsert:complete") < events.index("done")


def test_agentic_and_local_streams_emit_durable_ids_first(client, monkeypatch):
    session_id = _create_session(client)
    repo = client.app.state.session_repo
    original_upsert = repo.upsert_message
    events: list[str] = []
    _track_terminal_yields(monkeypatch, events)

    async def tracked_upsert(user_id, message):
        result = await original_upsert(user_id, message)
        events.append(f"upsert:{message.status.value}")
        return result

    monkeypatch.setattr(repo, "upsert_message", tracked_upsert)
    agentic = client.post(
        "/api/chat",
        json={
            "sessionId": session_id,
            "content": "@analyst calculate 6*7",
            "stream": True,
        },
    )
    agentic_payloads = _sse_payloads(agentic.text)
    assert "metadata" in json.loads(agentic_payloads[0])
    assert events.index("upsert:complete") < events.index("done")

    events.clear()
    original_add = repo.add_message

    async def tracked_add(user_id, message):
        result = await original_add(user_id, message)
        if message.role is MessageRole.assistant:
            events.append("add:assistant")
        return result

    monkeypatch.setattr(repo, "add_message", tracked_add)
    local_session_id = _create_session(client)
    local = client.post(
        "/api/chat",
        json={"sessionId": local_session_id, "content": "/help", "stream": True},
    )
    local_payloads = _sse_payloads(local.text)
    local_messages = client.get(
        f"/api/sessions/{local_session_id}/messages"
    ).json()
    assert json.loads(local_payloads[0])["metadata"] == {
        "userMessageId": local_messages[0]["id"],
        "assistantMessageId": local_messages[1]["id"],
    }
    assert local_payloads[-1] == "[DONE]"
    assert events.index("add:assistant") < events.index("done")


def test_superseded_summary_stream_has_no_assistant_metadata_or_done(client, monkeypatch):
    session_id = _create_session(client)
    for content in (
        "first real user turn with enough detail to summarize",
        "second real user turn with additional context for the summary",
    ):
        response = client.post(
            "/api/chat",
            json={"sessionId": session_id, "content": content, "stream": False},
        )
        assert response.status_code == 200

    async def suppress_reply(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        client.app.state.session_repo,
        "add_message_if_summary_version",
        suppress_reply,
    )
    response = client.post(
        "/api/chat",
        json={"sessionId": session_id, "content": "/summarize", "stream": True},
    )
    payloads = _sse_payloads(response.text)

    assert len(payloads) == 1
    assert json.loads(payloads[0]) == {
        "error": "The command result was superseded before it could be saved.",
        "persistenceSuppressed": True,
    }
    assert "[DONE]" not in payloads
    assert "metadata" not in payloads[0]


def test_plain_tool_stream_disconnect_before_iteration_has_no_placeholder(
    client, monkeypatch
):
    class WebSearch:
        def build_capability(self, **_kwargs):
            async def handler(_arguments):
                return {"ok": True}

            return (
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "description": "Search",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                {"web_search": handler},
            )

    class DisconnectBeforeIteration(StreamingResponse):
        async def __call__(self, _scope, _receive, send):
            await self.body_iterator.aclose()  # type: ignore[attr-defined]
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
            await send({"type": "http.response.body", "body": b""})

    client.app.state.web_search = WebSearch()
    monkeypatch.setattr(chat_router, "StreamingResponse", DisconnectBeforeIteration)
    session_id = _create_session(client)
    response = client.post(
        "/api/chat",
        json={"sessionId": session_id, "content": "find current news", "stream": True},
    )

    assert response.status_code == 200
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert [message["role"] for message in messages] == ["user"]


def test_persistence_failure_is_explicit_and_never_reports_done(client, monkeypatch):
    session_id = _create_session(client)
    repo = client.app.state.session_repo

    async def fail_upsert(_user_id, _message):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(repo, "upsert_message", fail_upsert)
    response = client.post(
        "/api/chat",
        json={"sessionId": session_id, "content": "hello", "stream": True},
    )
    payloads = _sse_payloads(response.text)
    error = json.loads(payloads[-1])
    assert payloads[0].startswith('{"metadata"')
    assert "[DONE]" not in payloads
    assert error["persistenceFailed"] is True


def test_gateway_error_is_persisted_before_terminal_error(client, monkeypatch):
    class FailingGateway:
        async def stream(self, **_kwargs):
            raise ModelGatewayError(502, "gateway failed")
            yield  # pragma: no cover

    session_id = _create_session(client)
    client.app.state.gateway = FailingGateway()
    repo = client.app.state.session_repo
    original_upsert = repo.upsert_message
    events: list[str] = []
    _track_terminal_yields(monkeypatch, events)

    async def tracked_upsert(user_id, message):
        result = await original_upsert(user_id, message)
        events.append(f"upsert:{message.status.value}")
        return result

    monkeypatch.setattr(repo, "upsert_message", tracked_upsert)
    response = client.post(
        "/api/chat",
        json={"sessionId": session_id, "content": "hello", "stream": True},
    )
    payloads = _sse_payloads(response.text)
    assert events == ["upsert:error", "error"]
    assert json.loads(payloads[-1])["error"] == "gateway failed"
    assert "[DONE]" not in payloads


@pytest.mark.asyncio
async def test_agentic_stream_close_persists_cancelled_state():
    class Repo:
        def __init__(self) -> None:
            self.persisted: list[Message] = []

        async def upsert_message(self, _user_id, message):
            self.persisted.append(message.model_copy(deep=True))
            return message

    class Memory:
        async def remember(self, *_args, **_kwargs):
            return None

    class Metering:
        async def record_completion(self, **_kwargs):
            return None

    async def run(_on_step):
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    repo = Repo()
    assistant = Message(
        sessionId="session",
        userId="user",
        role=MessageRole.assistant,
        status=MessageStatus.streaming,
    )
    stream = _agentic_stream(
        assistant=assistant,
        run=run,
        repo=repo,  # type: ignore[arg-type]
        memory=Memory(),  # type: ignore[arg-type]
        metering=Metering(),  # type: ignore[arg-type]
        user=AuthenticatedUser(
            internal_user_id="user",
            subject="subject",
            issuer="issuer",
            provider="dev",
            email="user@example.com",
            name="User",
        ),
        session_id="session",
        model_id="model",
        deployment=DeploymentOption(
            region="eastus",
            dataZone=None,
            sku="GlobalStandard",
            deploymentName="deployment",
        ),
        agent_name="analyst",
        correlation_id="correlation",
        content_for_model="hello",
        user_message_id="user-message",
    )
    assert "metadata" in await anext(stream)
    await stream.aclose()
    assert repo.persisted[-1].status is MessageStatus.cancelled


@pytest.mark.asyncio
async def test_unstarted_stream_does_not_persist_placeholder():
    class Repo:
        def __init__(self) -> None:
            self.added: list[Message] = []

        async def add_message(self, _user_id, message):
            self.added.append(message)
            return message

    async def body():
        yield "data: never\n\n"

    repo = Repo()
    assistant = Message(
        sessionId="session",
        userId="user",
        role=MessageRole.assistant,
        status=MessageStatus.streaming,
    )
    stream = _stream_with_placeholder(
        repo=repo,  # type: ignore[arg-type]
        user_id="user",
        assistant=assistant,
        events=body(),
    )
    await stream.aclose()
    assert repo.added == []


@pytest.mark.asyncio
async def test_placeholder_failure_is_an_explicit_stream_error():
    class Repo:
        async def add_message(self, _user_id, _message):
            raise RuntimeError("store unavailable")

    async def body():
        yield "data: never\n\n"

    assistant = Message(
        sessionId="session",
        userId="user",
        role=MessageRole.assistant,
        status=MessageStatus.streaming,
    )
    stream = _stream_with_placeholder(
        repo=Repo(),  # type: ignore[arg-type]
        user_id="user",
        assistant=assistant,
        events=body(),
    )
    payload = json.loads((await anext(stream)).removeprefix("data: "))
    assert payload == {
        "error": "The reply could not be initialized.",
        "persistenceFailed": True,
    }
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_disconnect_before_first_event_finalizes_owned_placeholder():
    class Repo:
        def __init__(self) -> None:
            self.added: list[Message] = []
            self.persisted: list[Message] = []

        async def add_message(self, _user_id, message):
            self.added.append(message.model_copy(deep=True))
            return message

        async def upsert_message(self, _user_id, message):
            self.persisted.append(message.model_copy(deep=True))
            return message

    body_started = asyncio.Event()

    async def body():
        body_started.set()
        await asyncio.sleep(60)
        yield "data: never\n\n"

    repo = Repo()
    assistant = Message(
        sessionId="session",
        userId="user",
        role=MessageRole.assistant,
        status=MessageStatus.streaming,
    )
    stream = _stream_with_placeholder(
        repo=repo,  # type: ignore[arg-type]
        user_id="user",
        assistant=assistant,
        events=body(),
    )
    first_event = asyncio.create_task(anext(stream))
    await body_started.wait()
    first_event.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_event

    assert repo.added[-1].status is MessageStatus.streaming
    assert repo.persisted[-1].status is MessageStatus.cancelled


@pytest.mark.asyncio
async def test_cancellation_during_terminal_upsert_confirms_then_persists_cancelled():
    class Repo:
        def __init__(self) -> None:
            self.persisted: list[Message] = []
            self.first_write_started = asyncio.Event()
            self.release_first_write = asyncio.Event()
            self.write_count = 0

        async def upsert_message(self, _user_id, message):
            self.write_count += 1
            if self.write_count == 1:
                self.first_write_started.set()
                await self.release_first_write.wait()
            self.persisted.append(message.model_copy(deep=True))
            return message

    class Memory:
        async def remember(self, *_args, **_kwargs):
            return None

    class Metering:
        async def record_completion(self, **_kwargs):
            return None

    async def run(_on_step):
        return AgentRunResult(text="answer", model="deployment")

    repo = Repo()
    assistant = Message(
        sessionId="session",
        userId="user",
        role=MessageRole.assistant,
        status=MessageStatus.streaming,
    )
    stream = _agentic_stream(
        assistant=assistant,
        run=run,
        repo=repo,  # type: ignore[arg-type]
        memory=Memory(),  # type: ignore[arg-type]
        metering=Metering(),  # type: ignore[arg-type]
        user=AuthenticatedUser(
            internal_user_id="user",
            subject="subject",
            issuer="issuer",
            provider="dev",
            email="user@example.com",
            name="User",
        ),
        session_id="session",
        model_id="model",
        deployment=DeploymentOption(
            region="eastus",
            dataZone=None,
            sku="GlobalStandard",
            deploymentName="deployment",
        ),
        agent_name="analyst",
        correlation_id="correlation",
        content_for_model="hello",
        user_message_id="user-message",
    )
    assert "metadata" in await anext(stream)
    assert "answer" in await anext(stream)

    terminal = asyncio.create_task(anext(stream))
    await repo.first_write_started.wait()
    terminal.cancel()
    repo.release_first_write.set()
    with pytest.raises(asyncio.CancelledError):
        await terminal

    assert [message.status for message in repo.persisted] == [
        MessageStatus.complete,
        MessageStatus.cancelled,
    ]
