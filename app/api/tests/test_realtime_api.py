"""Voice Live WebSocket relay contract + governance.

Exercises the ``/api/voice/live`` route end to end with a fake upstream socket
(no network): the disabled-by-default refusal, Origin/auth/entitlement denials,
and the happy-path bidirectional pump + per-session metering. The upstream
connector is swapped on ``app.state`` exactly like the REST voice tests swap the
gateway.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ai4ia_api.main import create_app
from ai4ia_api.routers.realtime import (
    DEV_SUBPROTOCOL,
    UpstreamMessage,
)
from tests.conftest import make_settings

ADMIN = {"X-Dev-User": "alice"}


def _origin(value: str = "http://localhost:3000") -> dict[str, str]:
    # A fresh dict per call: Starlette's ``websocket_connect`` mutates the passed
    # headers (``setdefault('sec-websocket-protocol', ...)``), so a shared dict
    # would leak one test's subprotocols into the next.
    return {"origin": value}


class FakeUpstream:
    """In-memory echo socket: every client frame comes back as ``echo:<frame>``."""

    def __init__(self) -> None:
        import asyncio

        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed = False
        self._queue: asyncio.Queue[UpstreamMessage] = asyncio.Queue()

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)
        await self._queue.put(UpstreamMessage("text", text=f"echo:{data}"))

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)
        await self._queue.put(UpstreamMessage("binary", data=b"echo:" + data))

    async def receive(self) -> UpstreamMessage:
        return await self._queue.get()

    async def close(self) -> None:
        self.closed = True


class FakeRealtimeConnector:
    """Injectable connector capturing connect args; optionally fails to connect."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.upstream = FakeUpstream()
        self.connects: list[dict] = []

    @asynccontextmanager
    async def connect(self, *, url: str, headers: dict[str, str], timeout: float):
        self.connects.append({"url": url, "headers": headers, "timeout": timeout})
        if self.fail:
            raise RuntimeError("upstream unreachable")
        try:
            yield self.upstream
        finally:
            await self.upstream.close()


class FakeUsageService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def record_completion(self, **kwargs):
        self.calls.append(kwargs)

    async def summarize(self, *args, **kwargs):  # pragma: no cover - not used here
        raise AssertionError("summarize should not be called in this test")

    async def close(self) -> None:
        return None


def _client(**overrides) -> TestClient:
    defaults = {
        "model_gateway_auth_mode": "api_key",
        "model_gateway_api_key": "proxy-ingress-key",
        "realtime_base_url": "https://realtime-gateway.test/openai",
        "realtime_gateway_api_key": "realtime-key",
    }
    defaults.update(overrides)
    settings = make_settings(admin_subjects="alice", **defaults)
    c = TestClient(app := create_app(settings))
    c.__enter__()
    c.app.state.realtime_connector = FakeRealtimeConnector()
    assert app is c.app
    return c


def _speech_client(**overrides) -> TestClient:
    defaults = {
        "realtime_enabled": True,
        "speech_voice_live_enabled": True,
        "voice_provider_allowlist": "azure_openai,speech_voice_live",
        "speech_voice_live_base_url": "https://speech-gateway.test/speech/voice-live",
        "speech_voice_live_gateway_api_key": "speech-key",
    }
    defaults.update(overrides)
    return _client(**defaults)


@pytest.fixture
def client():
    c = _client(realtime_enabled=True)
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _internal_id(client, headers) -> str:
    return client.get("/api/entitlement", headers=headers).json()["userId"]


# --------------------------------------------------------------------------- #
# Disabled by default (zero-regression posture).
# --------------------------------------------------------------------------- #


def test_live_disabled_by_default_refuses():
    # No realtime_enabled override -> defaults OFF -> route refuses before accept.
    c = _client()
    try:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(
                "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "u"], headers=_origin()
            ):
                pass
    finally:
        c.__exit__(None, None, None)


def test_live_unknown_provider_rejected_before_connect():
    c = _client(realtime_enabled=True)
    try:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(
                "/api/voice/live?provider=no-such-provider",
                subprotocols=[DEV_SUBPROTOCOL, "u"],
                headers=_origin(),
            ):
                pass
        assert c.app.state.realtime_connector.connects == []
    finally:
        c.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# Auth / Origin / entitlement denials.
# --------------------------------------------------------------------------- #


def test_live_missing_auth_subprotocol_refused(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/voice/live", headers=_origin()):
            pass


def test_live_origin_rejected_when_allowlist_set():
    c = _client(realtime_enabled=True, realtime_allowed_origins="https://good.example")
    try:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(
                "/api/voice/live",
                subprotocols=[DEV_SUBPROTOCOL, "u"],
                headers={"origin": "https://evil.example"},
            ):
                pass
    finally:
        c.__exit__(None, None, None)


def test_live_disabled_user_refused(client):
    headers = {"X-Dev-User": "banned"}
    uid = _internal_id(client, headers)
    client.put(f"/api/admin/entitlements/{uid}", json={"disabled": True}, headers=ADMIN)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "banned"], headers=_origin()
        ):
            pass


def test_live_speech_enforces_shared_auth_origin_and_entitlement_before_connect():
    cases = [
        ({}, [], _origin()),
        (
            {"realtime_allowed_origins": "https://good.example"},
            [DEV_SUBPROTOCOL, "u"],
            {"origin": "https://evil.example"},
        ),
    ]
    for overrides, subprotocols, headers in cases:
        c = _speech_client(**overrides)
        try:
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect(
                    "/api/voice/live?provider=speech_voice_live",
                    subprotocols=subprotocols,
                    headers=headers,
                ):
                    pass
            assert c.app.state.realtime_connector.connects == []
        finally:
            c.__exit__(None, None, None)

    c = _speech_client()
    try:
        headers = {"X-Dev-User": "speech-banned"}
        uid = _internal_id(c, headers)
        c.put(f"/api/admin/entitlements/{uid}", json={"disabled": True}, headers=ADMIN)
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(
                "/api/voice/live?provider=speech_voice_live",
                subprotocols=[DEV_SUBPROTOCOL, "speech-banned"],
                headers=_origin(),
            ):
                pass
        assert c.app.state.realtime_connector.connects == []
    finally:
        c.__exit__(None, None, None)


def test_live_upstream_failure_closes(client):
    client.app.state.realtime_connector = FakeRealtimeConnector(fail=True)
    with client.websocket_connect(
        "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "u"], headers=_origin()
    ) as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


# --------------------------------------------------------------------------- #
# Happy path: bidirectional pump + governance side effects.
# --------------------------------------------------------------------------- #


def test_live_relay_pumps_both_directions(client):
    connector = client.app.state.realtime_connector
    with client.websocket_connect(
        "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "liveuser"], headers=_origin()
    ) as ws:
        ws.send_text('{"type":"session.update"}')
        assert ws.receive_text() == 'echo:{"type":"session.update"}'
        ws.send_bytes(b"\x01\x02pcm")
        assert ws.receive_bytes() == b"echo:\x01\x02pcm"

    # Upstream was opened exactly once with the gateway-derived URL + credential.
    assert len(connector.connects) == 1
    opened = connector.connects[0]
    assert opened["url"].startswith("wss://realtime-gateway.test/openai/realtime")
    assert "deployment=" in opened["url"]
    assert connector.upstream.sent_text == ['{"type":"session.update"}']
    assert connector.upstream.sent_bytes == [b"\x01\x02pcm"]
    assert connector.upstream.closed is True


def test_live_speech_provider_uses_fixed_upstream_and_normalizes_session():
    c = _speech_client(realtime_tools_enabled=True)
    try:
        connector = ToolFakeConnector()
        c.app.state.realtime_connector = connector
        with c.websocket_connect(
            "/api/voice/live?provider=speech_voice_live&model=gpt-realtime&region=eastus2"
            "&tools=1&host=attacker.example&path=/other&api-version=future"
            "&deployment=attacker-deployment&customVoice=secret",
            subprotocols=[DEV_SUBPROTOCOL, "speechuser"],
            headers=_origin(),
        ) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "session.update",
                        "host": "attacker.example",
                        "api-version": "future",
                        "model": "attacker-model",
                        "deployment": "attacker-deployment",
                        "session": {
                            "voice": {
                                "type": "azure-standard",
                                "name": "en-US-AndrewNeural",
                                "endpointId": "custom-endpoint",
                            },
                            "input_audio_transcription": {"model": "azure-speech"},
                            "turn_detection": {
                                "type": "azure_semantic_vad_multilingual",
                                "interrupt_response": True,
                                "auto_truncate": False,
                            },
                            "locale": "en-US",
                            "voiceEndpointId": "custom-endpoint",
                            "lexicons": ["bad"],
                            "personalVoice": {"name": "bad"},
                            "tools": [{"type": "function", "name": "untrusted"}],
                        },
                    }
                )
            )
            fc = json.loads(ws.receive_text())
            assert fc["type"] == "response.function_call_arguments.done"
            assert json.loads(ws.receive_text())["type"] == "response.done"

        opened = connector.connects[0]
        assert opened["url"] == (
            "wss://speech-gateway.test/speech/voice-live/realtime"
            "?api-version=2026-04-10&model=gpt-realtime"
        )
        assert opened["headers"]["Ocp-Apim-Subscription-Key"] == "speech-key"

        sent = json.loads(connector.upstream.sent_text[0])
        session = sent["session"]
        assert session["voice"] == {
            "type": "azure-standard",
            "name": "en-US-AndrewNeural",
            "locale": "en-US",
        }
        assert session["input_audio_transcription"] == {
            "model": "gpt-4o-transcribe",
            "language": "en-US",
        }
        assert session["turn_detection"] == {
            "type": "azure_semantic_vad_multilingual",
            "create_response": True,
            "interrupt_response": True,
            "auto_truncate": False,
        }
        assert session["input_audio_noise_reduction"] == {
            "type": "azure_deep_noise_suppression"
        }
        assert session["input_audio_echo_cancellation"] == {
            "type": "server_echo_cancellation"
        }
        assert session["input_audio_sampling_rate"] == 24_000
        assert "voiceEndpointId" not in session
        assert "lexicons" not in session
        assert "personalVoice" not in session
        assert "host" not in sent
        assert "api-version" not in sent
        assert "model" not in sent
        assert "deployment" not in sent
        assert session["tools"]
        assert all(tool.get("name") != "untrusted" for tool in session["tools"])
        assert connector.upstream.closed is True
    finally:
        c.__exit__(None, None, None)


def test_live_speech_rejects_nonmatching_model_or_region():
    c = _speech_client()
    try:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(
                "/api/voice/live?provider=speech_voice_live&model=wrong&region=westus",
                subprotocols=[DEV_SUBPROTOCOL, "u"],
                headers=_origin(),
            ):
                pass
        assert c.app.state.realtime_connector.connects == []
    finally:
        c.__exit__(None, None, None)


def test_live_speech_reconstructs_response_create_before_forwarding():
    c = _speech_client()
    try:
        connector = FakeRealtimeConnector()
        c.app.state.realtime_connector = connector
        with c.websocket_connect(
            "/api/voice/live?provider=speech_voice_live",
            subprotocols=[DEV_SUBPROTOCOL, "u"],
            headers=_origin(),
        ) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "response.create",
                        "response": {
                            "voice": {
                                "type": "azure-custom",
                                "name": "private",
                                "endpoint_id": "custom-endpoint",
                            },
                            "tools": [{"type": "function", "name": "untrusted"}],
                        },
                    }
                )
            )
            assert json.loads(ws.receive_text().removeprefix("echo:")) == {
                "type": "response.create"
            }
        assert [json.loads(frame) for frame in connector.upstream.sent_text] == [
            {"type": "response.create"}
        ]
    finally:
        c.__exit__(None, None, None)


def test_live_speech_upstream_failure_is_bounded_and_cleans_up():
    c = _speech_client()
    try:
        connector = FakeRealtimeConnector(fail=True)
        c.app.state.realtime_connector = connector
        with c.websocket_connect(
            "/api/voice/live?provider=speech_voice_live",
            subprotocols=[DEV_SUBPROTOCOL, "u"],
            headers=_origin(),
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == 1011
        assert len(connector.connects) == 1
        opened = connector.connects[0]
        assert opened["url"].endswith(
            "/speech/voice-live/realtime?api-version=2026-04-10&model=gpt-realtime"
        )
        assert opened["headers"]["Ocp-Apim-Subscription-Key"] == "speech-key"
    finally:
        c.__exit__(None, None, None)


def test_live_config_exposes_safe_provider_catalog():
    c = _speech_client()
    try:
        response = c.get("/api/voice/live/config")
        assert response.status_code == 200
        body = response.json()
        assert body["defaultProviderId"] == "azure_openai"
        assert body["enabledProviderIds"] == ["azure_openai", "speech_voice_live"]
        providers = {provider["id"]: provider for provider in body["providers"]}
        assert "endpointPath" not in providers["speech_voice_live"]
        assert "managedModel" in providers["speech_voice_live"]
        assert providers["speech_voice_live"]["capabilities"]["voices"]["kind"] == "azure-standard"
        assert providers["speech_voice_live"]["capabilities"]["inputTranscription"] == {
            "provider": "openai",
            "default": "gpt-4o-transcribe",
            "options": ["gpt-4o-transcribe"],
        }
    finally:
        c.__exit__(None, None, None)


def test_live_session_is_metered(client):
    headers = {"X-Dev-User": "meterlive"}
    with client.websocket_connect(
        "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "meterlive"], headers=_origin()
    ) as ws:
        ws.send_text("ping")
        ws.receive_text()

    summary = client.get("/api/usage", headers=headers).json()
    assert summary["totalRequests"] >= 1


def test_live_speech_session_records_managed_voice_usage():
    c = _speech_client()
    try:
        headers = {"X-Dev-User": "speechmeter"}
        uid = _internal_id(c, headers)
        usage = FakeUsageService()
        c.app.state.usage = usage
        c.app.state.realtime_connector = FakeRealtimeConnector()
        with c.websocket_connect(
            "/api/voice/live?provider=speech_voice_live",
            subprotocols=[DEV_SUBPROTOCOL, "speechmeter"],
            headers=_origin(),
        ) as ws:
            ws.send_text('{"type":"input_audio_buffer.commit"}')
            assert json.loads(ws.receive_text().removeprefix("echo:")) == {
                "type": "input_audio_buffer.commit"
            }

        assert len(usage.calls) == 1
        call = usage.calls[0]
        assert call["user_id"] == uid
        assert call["session_id"] == "voice-live"
        assert call["model_id"] == "gpt-realtime"
        assert call["status"] == "complete"
        assert call["usage"].known is False
        assert call["usage"].complete is False
        assert call["usage"].calls == 1
        target = call["target"]
        assert target.provider == "speech_voice_live"
        assert target.deployment is None
        assert target.target == "managed_voice_live"
        assert target.region == "eastus2"
    finally:
        c.__exit__(None, None, None)


def test_live_accepts_matching_origin_with_allowlist():
    c = _client(realtime_enabled=True, realtime_allowed_origins="https://good.example")
    try:
        with c.websocket_connect(
            "/api/voice/live",
            subprotocols=[DEV_SUBPROTOCOL, "u"],
            headers={"origin": "https://good.example"},
        ) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "echo:hello"
    finally:
        c.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# Governed tool calling end to end (relay executes a function call in-process).
# --------------------------------------------------------------------------- #


class ToolFakeUpstream:
    """Fake upstream that drives a function call after the session is configured.

    On the tool-injected ``session.update`` it emits a ``calculator`` function-call
    event; on the relay's follow-up ``response.create`` it emits a ``response.done``
    sync point. Every frame is also recorded so the test can assert the relay sent
    the tool result back upstream.
    """

    def __init__(self) -> None:
        import asyncio

        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed = False
        self._queue: asyncio.Queue[UpstreamMessage] = asyncio.Queue()

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)
        if '"session.update"' in data:
            await self._queue.put(
                UpstreamMessage(
                    "text",
                    text=(
                        '{"type":"response.function_call_arguments.done",'
                        '"call_id":"call_1","name":"calculator",'
                        '"arguments":"{\\"expression\\":\\"2+3\\"}"}'
                    ),
                )
            )
        elif '"response.create"' in data:
            await self._queue.put(UpstreamMessage("text", text='{"type":"response.done"}'))

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def receive(self) -> UpstreamMessage:
        return await self._queue.get()

    async def close(self) -> None:
        self.closed = True


class ToolFakeConnector:
    def __init__(self) -> None:
        self.upstream = ToolFakeUpstream()
        self.connects: list[dict] = []

    @asynccontextmanager
    async def connect(self, *, url: str, headers: dict[str, str], timeout: float):
        self.connects.append({"url": url, "headers": headers, "timeout": timeout})
        try:
            yield self.upstream
        finally:
            await self.upstream.close()


def test_live_tool_call_executed_and_returned_upstream():
    import json

    c = _client(realtime_enabled=True, realtime_tools_enabled=True)
    try:
        connector = ToolFakeConnector()
        c.app.state.realtime_connector = connector
        with c.websocket_connect(
            "/api/voice/live?tools=1",
            subprotocols=[DEV_SUBPROTOCOL, "tooluser"],
            headers=_origin(),
        ) as ws:
            ws.send_text('{"type":"session.update","session":{"voice":"verse"}}')
            # The browser still observes the model's function-call event (forwarded).
            fc = json.loads(ws.receive_text())
            assert fc["type"] == "response.function_call_arguments.done"
            # Then the relay's tool result prompts a response; we get the sync point.
            assert json.loads(ws.receive_text())["type"] == "response.done"

        sent = connector.upstream.sent_text
        # 1) The session.update the relay forwarded carries the injected tools.
        injected = json.loads(sent[0])
        assert injected["session"]["voice"] == "verse"  # client field preserved
        names = {t["name"] for t in injected["session"]["tools"]}
        assert "calculator" in names
        assert injected["session"]["tool_choice"] == "auto"
        # 2) The relay sent a function_call_output with the computed result, then
        #    a response.create.
        output_frame = json.loads(sent[1])
        assert output_frame["item"]["type"] == "function_call_output"
        assert output_frame["item"]["call_id"] == "call_1"
        assert json.loads(output_frame["item"]["output"])["result"] == 5
        assert json.loads(sent[2]) == {"type": "response.create"}
    finally:
        c.__exit__(None, None, None)


def test_live_tools_disabled_does_not_inject_or_execute():
    # realtime_enabled but tools OFF -> relay stays a transparent pump: the
    # session.update is forwarded byte-for-byte and no tool frames are injected.
    import json

    c = _client(realtime_enabled=True)
    try:
        connector = ToolFakeConnector()
        c.app.state.realtime_connector = connector
        with c.websocket_connect(
            "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "u"], headers=_origin()
        ) as ws:
            ws.send_text('{"type":"session.update","session":{"voice":"verse"}}')
            # The function-call event is still forwarded to the browser...
            assert json.loads(ws.receive_text())["type"] == (
                "response.function_call_arguments.done"
            )

        sent = connector.upstream.sent_text
        # ...but the relay neither rewrote the session.update nor replied to the call.
        assert sent == ['{"type":"session.update","session":{"voice":"verse"}}']
    finally:
        c.__exit__(None, None, None)


def test_live_tools_flag_on_but_no_opt_in_stays_passthrough():
    # The server flag is ON, but the browser did NOT opt in (?tools= absent). The
    # per-session opt-in defaults OFF, so the relay stays a transparent pump: the
    # session.update is forwarded byte-for-byte and no tool frames are injected.
    # This is the default-OFF safety guarantee for tools in voice.
    import json

    c = _client(realtime_enabled=True, realtime_tools_enabled=True)
    try:
        connector = ToolFakeConnector()
        c.app.state.realtime_connector = connector
        with c.websocket_connect(
            "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "u"], headers=_origin()
        ) as ws:
            ws.send_text('{"type":"session.update","session":{"voice":"verse"}}')
            assert json.loads(ws.receive_text())["type"] == (
                "response.function_call_arguments.done"
            )

        sent = connector.upstream.sent_text
        assert sent == ['{"type":"session.update","session":{"voice":"verse"}}']
    finally:
        c.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# Agent-aware live voice end to end: ?agent= binds persona + scoped tools.
# --------------------------------------------------------------------------- #


def test_live_agent_binds_persona_and_scopes_tools():
    # ?agent=analyst (a curated agent with tools=["calculator"]) + tools enabled:
    # the forwarded session.update carries the analyst persona instructions and
    # ONLY the calculator tool (not get_current_time).
    import json

    c = _client(realtime_enabled=True, realtime_tools_enabled=True)
    try:
        connector = FakeRealtimeConnector()
        c.app.state.realtime_connector = connector
        with c.websocket_connect(
            "/api/voice/live?agent=analyst&tools=1",
            subprotocols=[DEV_SUBPROTOCOL, "u"],
            headers=_origin(),
        ) as ws:
            ws.send_text('{"type":"session.update","session":{"voice":"verse"}}')
            ws.receive_text()  # echo of the (rewritten) session.update

        injected = json.loads(connector.upstream.sent_text[0])
        assert injected["session"]["voice"] == "verse"  # client field preserved
        assert injected["session"]["instructions"].startswith("You are AI4IA's Data Analyst")
        assert {t["name"] for t in injected["session"]["tools"]} == {"calculator"}
        assert injected["session"]["tool_choice"] == "auto"
    finally:
        c.__exit__(None, None, None)


def test_live_agent_persona_only_when_tools_disabled():
    # ?agent=coder (no tools) with realtime tools OFF: the relay still binds the
    # persona instructions but advertises no tools (persona-only voice agent).
    import json

    c = _client(realtime_enabled=True)
    try:
        connector = FakeRealtimeConnector()
        c.app.state.realtime_connector = connector
        with c.websocket_connect(
            "/api/voice/live?agent=coder",
            subprotocols=[DEV_SUBPROTOCOL, "u"],
            headers=_origin(),
        ) as ws:
            ws.send_text('{"type":"session.update","session":{"voice":"verse"}}')
            ws.receive_text()

        injected = json.loads(connector.upstream.sent_text[0])
        assert injected["session"]["instructions"].startswith("You are AI4IA's Code Assistant")
        assert "tools" not in injected["session"]
    finally:
        c.__exit__(None, None, None)


def test_live_unknown_agent_falls_back_to_generic_passthrough():
    # An unknown ?agent= must not break the session: it falls back to the generic
    # assistant, and with tools off the relay stays a byte-for-byte pump.
    c = _client(realtime_enabled=True)
    try:
        connector = FakeRealtimeConnector()
        c.app.state.realtime_connector = connector
        with c.websocket_connect(
            "/api/voice/live?agent=does-not-exist",
            subprotocols=[DEV_SUBPROTOCOL, "u"],
            headers=_origin(),
        ) as ws:
            ws.send_text('{"type":"session.update"}')
            assert ws.receive_text() == 'echo:{"type":"session.update"}'

        assert connector.upstream.sent_text == ['{"type":"session.update"}']
    finally:
        c.__exit__(None, None, None)
