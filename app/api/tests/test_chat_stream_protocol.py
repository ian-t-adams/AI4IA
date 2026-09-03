from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from ai4ia_api.agents.receipt import ReceiptDraft
from ai4ia_api.agents.runtime import AgentRunResult, AgentStep
from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.catalog import DeploymentOption
from ai4ia_api.gateway.client import ChatChunk, ModelGatewayError
from ai4ia_api.main import create_app
from ai4ia_api.routers import chat as chat_router
from ai4ia_api.routers._chat_streaming import (
    AGENT_EVENT_QUEUE_MAXSIZE,
    _agentic_stream,
    _stream_with_placeholder,
)
from ai4ia_api.safety import MessageSafety, SafetySignal
from ai4ia_api.usage.models import TokenUsage
from ai4ia_api.sessions.models import Message, MessageRole, MessageStatus
from tests.conftest import make_settings, stream_like_gateway


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


class _PersistingRepo:
    def __init__(self) -> None:
        self.persisted: list[Message] = []

    async def upsert_message(self, _user_id, message):
        self.persisted.append(message.model_copy(deep=True))
        return message


class _NoopMemory:
    async def remember(self, *_args, **_kwargs):
        return None


class _NoopMetering:
    async def record_completion(self, **_kwargs):
        return None


def _stream_assistant() -> Message:
    return Message(
        sessionId="session",
        userId="user",
        role=MessageRole.assistant,
        status=MessageStatus.streaming,
    )


def _test_agentic_stream(
    *,
    run,
    repo,
    assistant: Message | None = None,
    memory=None,
    metering=None,
    **overrides,
):
    return _agentic_stream(
        assistant=assistant if assistant is not None else _stream_assistant(),
        run=run,
        repo=repo,
        memory=memory if memory is not None else _NoopMemory(),
        metering=metering if metering is not None else _NoopMetering(),
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
        **overrides,
    )


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


# --- Streamed tool loop (audit finding P1-16) --------------------------------
#
# A turn that calls a tool used to run every model round trip to completion
# before emitting anything, so the user saw a blank bubble for the whole turn.
# The tests below pin the fix at the SSE layer, where the user actually
# experiences it, and they are written so that reverting the fix fails them:
# each one asserts that SEVERAL content deltas arrive BEFORE the tool result,
# not merely that the concatenated text is correct. A single terminal delta
# concatenates correctly too — that is precisely the defect.


class _ToolThenAnswerGateway:
    """Iteration 1 says something and calls the calculator; iteration 2 answers."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        if self.calls == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Let me work that out. ",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "calculator",
                                        "arguments": json.dumps({"expression": "6*7"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"role": "assistant", "content": "The answer is 42."}}]}

    async def stream(self, **kwargs):
        async for chunk in stream_like_gateway(await self.complete(**kwargs)):
            yield chunk


def _frames(response_text: str) -> list[tuple[str, object]]:
    """Classify each SSE frame in order: ('delta'|'step'|'metadata'|..., payload)."""
    out: list[tuple[str, object]] = []
    for payload in _sse_payloads(response_text):
        if payload == "[DONE]":
            out.append(("done", None))
            continue
        obj = json.loads(payload)
        if "metadata" in obj:
            out.append(("metadata", obj["metadata"]))
        elif "step" in obj:
            out.append((f"step:{obj['step']['kind']}", obj["step"]))
        elif "approvals" in obj:
            out.append(("approvals", obj["approvals"]))
        elif "error" in obj:
            out.append(("error", obj["error"]))
        elif "choices" in obj:
            out.append(("delta", obj["choices"][0]["delta"].get("content") or ""))
    return out


def _tool_client(**settings) -> TestClient:
    client = TestClient(create_app(make_settings(**settings)))
    client.__enter__()
    client.app.state.gateway = _ToolThenAnswerGateway()
    return client


def _tool_turn(client: TestClient) -> str:
    session = client.post("/api/sessions", json={"title": "Chat", "model": "gpt-5.2"})
    assert session.status_code == 201
    response = client.post(
        "/api/chat",
        json={
            "sessionId": session.json()["id"],
            "content": "@analyst compute 6*7",
            "stream": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.text


def test_tool_turn_streams_several_deltas_before_the_tool_runs():
    """The P1-16 assertion: text reaches the client before the tool result.

    Non-vacuity is the point of the ``> 1`` below. A stream that emitted one
    delta containing the whole answer would still concatenate correctly and
    would still arrive before ``[DONE]``; it would simply be the original bug.
    """
    client = _tool_client()
    try:
        frames = _frames(_tool_turn(client))
    finally:
        client.__exit__(None, None, None)
    kinds = [kind for kind, _ in frames]

    first_result = kinds.index("step:tool_result")
    deltas_before_tool = [k for k in kinds[:first_result] if k == "delta"]
    assert len(deltas_before_tool) > 1, kinds
    # ...and the tool's own activity is visible before the answer that follows it.
    assert kinds.index("step:tool_start") < first_result
    assert any(k == "delta" for k in kinds[first_result:])
    assert kinds[0] == "metadata" and kinds[-1] == "done"

    text = "".join(str(value) for kind, value in frames if kind == "delta")
    assert text == "Let me work that out. The answer is 42."


def test_a_streamed_tool_turn_persists_exactly_what_it_streamed_before_done(monkeypatch):
    """Terminal-row-before-DONE survives, and the row matches the wire."""
    client = _tool_client()
    try:
        repo = client.app.state.session_repo
        original_upsert = repo.upsert_message
        events: list[str] = []
        _track_terminal_yields(monkeypatch, events)

        async def tracked_upsert(user_id, message):
            result = await original_upsert(user_id, message)
            events.append(f"upsert:{message.status.value}")
            return result

        monkeypatch.setattr(repo, "upsert_message", tracked_upsert)
        body = _tool_turn(client)
        sessions = client.get("/api/sessions").json()
        session_id = sessions[0]["id"] if isinstance(sessions, list) else sessions["items"][0]["id"]
        messages = client.get(f"/api/sessions/{session_id}/messages").json()
    finally:
        client.__exit__(None, None, None)

    streamed = "".join(str(value) for kind, value in _frames(body) if kind == "delta")
    assert events.index("upsert:complete") < events.index("done")
    assert messages[-1]["content"] == streamed
    assert messages[-1]["status"] == "complete"


def test_the_kill_switch_restores_the_single_terminal_delta():
    """Server-authoritative: OFF genuinely puts the old bytes back on the wire.

    The web app is not consulted anywhere in this decision — the flag is read
    from server settings inside the chat router, so this is the only place the
    posture can be set.
    """
    client = _tool_client(gateway_stream_tool_loop=False)
    try:
        frames = _frames(_tool_turn(client))
    finally:
        client.__exit__(None, None, None)
    kinds = [kind for kind, _ in frames]

    deltas = [k for k in kinds if k == "delta"]
    assert len(deltas) == 1, kinds
    # The one delta lands AFTER the tool result, which is the defect the flag
    # exists to let an operator fall back to.
    assert kinds.index("delta") > kinds.index("step:tool_result")
    text = "".join(str(value) for kind, value in frames if kind == "delta")
    assert text == "The answer is 42."


@pytest.mark.asyncio
async def test_a_disconnect_mid_stream_persists_the_text_the_user_received():
    """Cancellation still leaves no half-written row — it leaves an honest one."""

    async def run(on_step, on_delta):
        await on_step(
            AgentStep(
                kind="tool_result",
                tool="calculator",
                arguments={"expression": "6*7"},
                result={"result": 42},
            )
        )
        await on_delta("half an ")
        await on_delta("answer")
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    repo = _PersistingRepo()
    stream = _test_agentic_stream(
        run=run,
        repo=repo,
        stream_tokens=True,
        receipt_draft=ReceiptDraft(correlation_id="correlation"),
    )
    assert "metadata" in await anext(stream)
    assert "tool_result" in await anext(stream)
    assert "half an " in await anext(stream)
    assert "answer" in await anext(stream)
    await stream.aclose()

    assert repo.persisted[-1].status is MessageStatus.cancelled
    assert repo.persisted[-1].content == "half an answer"
    receipt = repo.persisted[-1].executionReceipt
    assert receipt is not None
    assert [call.tool for call in receipt.toolCalls] == ["calculator"]


@pytest.mark.asyncio
async def test_disconnect_recovers_completed_result_queued_after_last_delta():
    async def run(_on_step, on_delta):
        await on_delta("complete text")
        return AgentRunResult(
            text="complete text",
            model="deployment",
            iterations=2,
            usage=TokenUsage(
                prompt=5,
                completion=3,
                total=8,
                known=True,
                complete=True,
                calls=2,
            ),
            safety=MessageSafety(
                signals=[
                    SafetySignal(
                        category="violence",
                        scope="completion",
                        severity="safe",
                        severityLevel=0,
                        modelCall=2,
                    )
                ],
                signalCount=1,
            ),
        )

    repo = _PersistingRepo()
    stream = _test_agentic_stream(
        run=run,
        repo=repo,
        stream_tokens=True,
        receipt_draft=ReceiptDraft(correlation_id="correlation"),
    )
    assert "metadata" in await anext(stream)
    assert "complete text" in await anext(stream)
    await stream.aclose()

    row = repo.persisted[-1]
    assert row.status is MessageStatus.cancelled
    assert row.executionReceipt is not None
    assert row.executionReceipt.iterations == 2
    assert row.executionReceipt.usage.calls == 2
    assert row.safety is not None
    assert row.safety.signals[0].modelCall == 2


@pytest.mark.asyncio
async def test_a_streamed_preamble_is_kept_when_the_fallback_completes_the_turn():
    """The user cannot un-see a preamble, so it must survive the fallback."""

    async def run(_on_step, on_delta):
        await on_delta("Looking that up. ")
        return AgentRunResult(text="", model="deployment")

    async def fallback():
        from ai4ia_api.usage.models import TokenUsage

        return (
            "Here is the answer.",
            TokenUsage.empty(),
            MessageSafety(
                provider="azure_openai",
                coverage=["completion"],
                signals=[
                    SafetySignal(
                        category="violence",
                        scope="completion",
                        severity="low",
                        severityLevel=1,
                    )
                ],
            ),
        )

    repo = _PersistingRepo()
    stream = _test_agentic_stream(
        run=run,
        repo=repo,
        fallback=fallback,
        stream_tokens=True,
    )
    frames = [frame async for frame in stream]
    delivered = "".join(
        json.loads(f.removeprefix("data: "))["choices"][0]["delta"]["content"]
        for f in frames
        if '"choices"' in f
    )
    assert delivered == "Looking that up. \n\nHere is the answer."
    assert repo.persisted[-1].safety is not None
    assert repo.persisted[-1].safety.signals[0].severityLevel == 1
    assert repo.persisted[-1].content == delivered
    assert frames[-1] == "data: [DONE]\n\n"


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
    assert json.loads(payloads[-1])["error"] == "Chat completion failed."
    assert "gateway failed" not in response.text
    assert "[DONE]" not in payloads


def test_raw_gateway_error_is_persisted_before_one_sanitized_error(
    client, monkeypatch
):
    class RawErrorGateway:
        async def stream(self, **_kwargs):
            delta = {"choices": [{"delta": {"content": "partial answer"}}]}
            yield ChatChunk(delta="partial answer", raw=json.dumps(delta))
            yield ChatChunk(
                raw=json.dumps(
                    {
                        "error": {
                            "message": "internal deployment secret",
                            "code": "backend_failure",
                        }
                    }
                )
            )
            yield ChatChunk(raw=json.dumps({"error": "duplicate"}))

    session_id = _create_session(client)
    client.app.state.gateway = RawErrorGateway()
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
    errors = [
        json.loads(payload)
        for payload in payloads
        if payload != "[DONE]" and "error" in json.loads(payload)
    ]

    assert events == ["upsert:error", "error"]
    assert errors == [{"error": "Model stream failed."}]
    assert "internal deployment secret" not in response.text
    assert "[DONE]" not in payloads
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert messages[-1]["status"] == "error"
    assert messages[-1]["content"] == "partial answer"


@pytest.mark.asyncio
async def test_agentic_stream_close_persists_cancelled_state():
    async def run(_on_step):
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    repo = _PersistingRepo()
    stream = _test_agentic_stream(
        run=run,
        repo=repo,
    )
    assert "metadata" in await anext(stream)
    await stream.aclose()
    assert repo.persisted[-1].status is MessageStatus.cancelled


@pytest.mark.asyncio
async def test_agentic_stream_backpressures_fast_producer_and_disconnect_unblocks_it(
    caplog,
):
    assert AGENT_EVENT_QUEUE_MAXSIZE == 32
    caplog.set_level(logging.INFO, logger="ai4ia_api.routers._chat_streaming")

    produced = 0
    cancelled = asyncio.Event()

    async def run(_on_step, on_delta):
        nonlocal produced
        try:
            for _ in range(42):
                await on_delta("x")
                produced += 1
            return AgentRunResult(text="done", model="deployment")
        finally:
            cancelled.set()

    repo = _PersistingRepo()
    stream = _test_agentic_stream(
        run=run,
        repo=repo,
        stream_tokens=True,
    )

    assert "metadata" in await anext(stream)
    await asyncio.sleep(0)
    assert produced == 32
    for _ in range(3):
        assert "choices" in await anext(stream)
        await asyncio.sleep(0)
    assert produced == 35
    backpressure_logs = [
        record
        for record in caplog.records
        if record.getMessage() == "chat.agent_stream_backpressure"
    ]
    assert len(backpressure_logs) == 1
    await asyncio.wait_for(stream.aclose(), timeout=1)
    await asyncio.wait_for(cancelled.wait(), timeout=1)
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
    stream = _stream_with_placeholder(
        repo=repo,  # type: ignore[arg-type]
        user_id="user",
        assistant=_stream_assistant(),
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

    stream = _stream_with_placeholder(
        repo=Repo(),  # type: ignore[arg-type]
        user_id="user",
        assistant=_stream_assistant(),
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
    stream = _stream_with_placeholder(
        repo=repo,  # type: ignore[arg-type]
        user_id="user",
        assistant=_stream_assistant(),
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

    async def run(_on_step):
        return AgentRunResult(text="answer", model="deployment")

    repo = Repo()
    stream = _test_agentic_stream(
        run=run,
        repo=repo,
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


@pytest.mark.asyncio
async def test_closing_agentic_stream_waits_for_runner_cleanup():
    started = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def run(_on_step):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            # Model clients may need asynchronous socket/response cleanup after
            # cancellation. The stream must not orphan that work.
            await asyncio.sleep(0.05)
            cleanup_finished.set()

    stream = _test_agentic_stream(
        run=run,
        repo=_PersistingRepo(),
    )

    assert "metadata" in await anext(stream)
    await started.wait()
    await stream.aclose()

    assert cleanup_finished.is_set()


def test_later_plain_tool_gateway_failure_persists_partial_stream_without_fallback(
    client, monkeypatch
):
    """A failed second iteration must terminate honestly after a successful tool."""
    handler_calls: list[dict] = []
    raw_argument = "argument-secret"
    raw_result = "result-secret"

    class WebSearch:
        async def close(self):
            return None

        def build_capability(self, **_kwargs):
            async def handler(arguments, _ctx):
                handler_calls.append(dict(arguments))
                return {"headlines": ["one"], "token": raw_result}

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

    class FailingSecondIterationGateway:
        def __init__(self) -> None:
            self.stream_calls = 0
            self.fallback_calls = 0

        async def stream(self, **_kwargs):
            self.stream_calls += 1
            if self.stream_calls == 1:
                first = {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Searching now. ",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "web_search",
                                            "arguments": json.dumps(
                                                {"authorization": raw_argument}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 13,
                        "completion_tokens": 2,
                        "total_tokens": 15,
                    },
                }
                async for chunk in stream_like_gateway(first):
                    yield chunk
                return
            delta = {"choices": [{"delta": {"content": "Almost done. "}}]}
            yield ChatChunk(delta="Almost done. ", raw=json.dumps(delta))
            raise ModelGatewayError(
                502, f"upstream echoed {raw_argument} and {raw_result}"
            )

        async def complete(self, **_kwargs):
            self.fallback_calls += 1
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "fallback answer",
                        },
                        "content_filter_results": {
                            "violence": {
                                "filtered": False,
                                "severity": "low",
                            }
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 99,
                    "completion_tokens": 1,
                    "total_tokens": 100,
                },
            }

    metered: list[dict] = []

    async def capture_usage(**kwargs):
        metered.append(kwargs)

    gateway = FailingSecondIterationGateway()
    client.app.state.web_search = WebSearch()
    client.app.state.gateway = gateway
    monkeypatch.setattr(client.app.state.usage, "record_completion", capture_usage)
    session_id = _create_session(client)

    response = client.post(
        "/api/chat",
        json={
            "sessionId": session_id,
            "content": "find current news",
            "stream": True,
        },
    )
    frames = _frames(response.text)
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assistant = messages[-1]
    delivered = "".join(str(value) for kind, value in frames if kind == "delta")
    tool_results = [
        step
        for step in assistant.get("steps") or []
        if step["kind"] in {"tool_result", "delegate"}
    ]
    usage = metered[-1]["usage"]

    assert {
        "status_code": response.status_code,
        "stream_calls": gateway.stream_calls,
        "handler_calls": handler_calls,
        "fallback_calls": gateway.fallback_calls,
        "delivered": delivered,
        "terminal_frames": [kind for kind, _ in frames if kind in {"error", "done"}],
        "public_error": next(value for kind, value in frames if kind == "error"),
        "assistant_status": assistant["status"],
        "assistant_content": assistant["content"],
        "tool_results": [(step["kind"], step["tool"]) for step in tool_results],
        "metered": (
            usage.prompt,
            usage.completion,
            usage.total,
            usage.calls,
            usage.complete,
            metered[-1]["status"],
        ),
    } == {
        "status_code": 200,
        "stream_calls": 2,
        "handler_calls": [{"authorization": raw_argument}],
        "fallback_calls": 0,
        "delivered": "Searching now. Almost done. ",
        "terminal_frames": ["error"],
        "public_error": "Chat completion failed.",
        "assistant_status": "error",
        "assistant_content": "Searching now. Almost done. ",
        "tool_results": [("delegate", "web_search")],
        "metered": (13, 2, 15, 2, False, "error"),
    }
    persisted = json.dumps(messages)
    assert raw_argument not in response.text and raw_argument not in persisted
    assert raw_result not in response.text and raw_result not in persisted


def test_first_plain_tool_stream_failure_without_partial_work_falls_back(
    client, monkeypatch, caplog
):
    marker = "stream-fallback-hostile-detail"

    class WebSearch:
        async def close(self):
            return None

        def build_capability(self, **_kwargs):
            async def handler(_arguments, _ctx):  # pragma: no cover
                raise AssertionError("no tool should run")

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

    class FirstCallFailureGateway:
        def __init__(self) -> None:
            self.stream_calls = 0
            self.fallback_calls = 0

        async def stream(self, **_kwargs):
            self.stream_calls += 1
            raise ModelGatewayError(502, marker)
            yield  # pragma: no cover

        async def complete(self, **_kwargs):
            self.fallback_calls += 1
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "fallback answer",
                        },
                        "content_filter_results": {
                            "violence": {
                                "filtered": False,
                                "severity": "low",
                            }
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                },
            }

    metered: list[dict] = []

    async def capture_usage(**kwargs):
        metered.append(kwargs)

    gateway = FirstCallFailureGateway()
    client.app.state.web_search = WebSearch()
    client.app.state.gateway = gateway
    monkeypatch.setattr(client.app.state.usage, "record_completion", capture_usage)
    session_id = _create_session(client)

    response = client.post(
        "/api/chat",
        json={"sessionId": session_id, "content": "find news", "stream": True},
    )
    frames = _frames(response.text)
    messages = client.get(f"/api/sessions/{session_id}/messages").json()

    assert gateway.stream_calls == 1
    assert gateway.fallback_calls == 1
    assert "".join(str(value) for kind, value in frames if kind == "delta") == "fallback answer"
    assert [kind for kind, _ in frames if kind in {"error", "done"}] == ["done"]
    assert messages[-1]["status"] == "complete"
    assert messages[-1]["content"] == "fallback answer"
    usage = metered[-1]["usage"]
    assert (
        usage.prompt,
        usage.completion,
        usage.total,
        usage.calls,
        usage.complete,
        metered[-1]["status"],
    ) == (5, 2, 7, 2, False, "complete")
    receipt = messages[-1]["executionReceipt"]
    assert receipt["iterations"] == 2
    assert receipt["usage"]["calls"] == 2
    assert messages[-1]["safety"]["signals"][0]["modelCall"] == 2
    assert marker not in caplog.text


def test_first_plain_tool_stream_failure_after_delta_is_partial_and_does_not_fallback(
    client, monkeypatch
):
    class WebSearch:
        async def close(self):
            return None

        def build_capability(self, **_kwargs):
            async def handler(_arguments, _ctx):  # pragma: no cover
                raise AssertionError("no tool should run")

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

    class PartialFirstCallFailureGateway:
        def __init__(self) -> None:
            self.stream_calls = 0
            self.fallback_calls = 0

        async def stream(self, **_kwargs):
            self.stream_calls += 1
            delta = {"choices": [{"delta": {"content": "partial answer"}}]}
            yield ChatChunk(delta="partial answer", raw=json.dumps(delta))
            raise ModelGatewayError(502, "hostile upstream detail")

        async def complete(self, **_kwargs):  # pragma: no cover
            self.fallback_calls += 1
            raise AssertionError("partial work must not fall back")

    metered: list[dict] = []

    async def capture_usage(**kwargs):
        metered.append(kwargs)

    gateway = PartialFirstCallFailureGateway()
    client.app.state.web_search = WebSearch()
    client.app.state.gateway = gateway
    monkeypatch.setattr(client.app.state.usage, "record_completion", capture_usage)
    session_id = _create_session(client)

    response = client.post(
        "/api/chat",
        json={"sessionId": session_id, "content": "find news", "stream": True},
    )
    frames = _frames(response.text)
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    usage = metered[-1]["usage"]

    assert gateway.stream_calls == 1
    assert gateway.fallback_calls == 0
    assert "".join(str(value) for kind, value in frames if kind == "delta") == "partial answer"
    assert [(kind, value) for kind, value in frames if kind in {"error", "done"}] == [
        ("error", "Chat completion failed.")
    ]
    assert messages[-1]["status"] == "error"
    assert messages[-1]["content"] == "partial answer"
    assert (usage.calls, usage.known, usage.complete, metered[-1]["status"]) == (
        1,
        False,
        False,
        "error",
    )
