"""Voice Live WebSocket relay contract + governance.

Exercises the ``/api/voice/live`` route end to end with a fake upstream socket
(no network): the disabled-by-default refusal, Origin/auth/entitlement denials,
and the happy-path bidirectional pump + per-session metering. The upstream
connector is swapped on ``app.state`` exactly like the REST voice tests swap the
gateway.
"""
from __future__ import annotations

import json
import logging
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


class ScriptedUpstream:
    def __init__(self, messages: list[UpstreamMessage]) -> None:
        import asyncio

        self._queue: asyncio.Queue[UpstreamMessage] = asyncio.Queue()
        for message in messages:
            self._queue.put_nowait(message)
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.close_calls = 0

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def receive(self) -> UpstreamMessage:
        return await self._queue.get()

    async def close(self) -> None:
        self.close_calls += 1


class ScriptedRealtimeConnector:
    def __init__(self, messages: list[UpstreamMessage]) -> None:
        self.upstream = ScriptedUpstream(messages)
        self.connects: list[dict] = []

    @asynccontextmanager
    async def connect(self, *, url: str, headers: dict[str, str], timeout: float):
        self.connects.append({"url": url, "headers": headers, "timeout": timeout})
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


class FailingUsageService(FakeUsageService):
    async def record_completion(self, **kwargs):
        await super().record_completion(**kwargs)
        raise RuntimeError("api_key=metering-secret")


def _completion_payloads(caplog) -> list[dict]:
    payloads = []
    for record in caplog.records:
        try:
            payload = json.loads(record.getMessage())
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("event") == "voice_live_completion":
            payloads.append(payload)
    return payloads


def _attach_completion_capture(caplog):
    target = logging.getLogger("ai4ia_api.routers.realtime")
    if caplog.handler in target.handlers or caplog.handler in logging.getLogger().handlers:
        return None
    target.addHandler(caplog.handler)
    target.setLevel(logging.INFO)
    return target


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


def test_live_upstream_failure_closes_and_records_once(client, caplog):
    caplog.set_level("INFO", logger="ai4ia_api.routers.realtime")
    capture = _attach_completion_capture(caplog)
    usage = FakeUsageService()
    try:
        client.app.state.usage = usage
        client.app.state.realtime_connector = FakeRealtimeConnector(fail=True)
        with client.websocket_connect(
            "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "u"], headers=_origin()
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == 1011
        assert len(usage.calls) == 1
        assert usage.calls[0]["status"] == "error"
        payloads = _completion_payloads(caplog)
        assert len(payloads) == 1
        assert payloads[0]["outcome"] == "error"
        assert payloads[0]["metadata"]["exceptionClass"] == "RuntimeError"
    finally:
        if capture is not None:
            capture.removeHandler(caplog.handler)


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


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-realtime",
        "gpt-realtime-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-5-mini",
        "gpt-5.1",
    ],
)
def test_live_speech_selected_model_controls_url_and_metering(model_id):
    c = _speech_client()
    try:
        connector = ScriptedRealtimeConnector(
            [UpstreamMessage("close", close_code=1000, source_event="CLOSE")]
        )
        usage = FakeUsageService()
        c.app.state.realtime_connector = connector
        c.app.state.usage = usage

        with c.websocket_connect(
            f"/api/voice/live?provider=speech_voice_live&model={model_id}&region=eastus2",
            subprotocols=[DEV_SUBPROTOCOL, "speechmodel"],
            headers=_origin(),
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()

        assert exc.value.code == 1000
        assert connector.connects[0]["url"] == (
            "wss://speech-gateway.test/speech/voice-live/realtime"
            f"?api-version=2026-04-10&model={model_id}"
        )
        assert connector.upstream.close_calls == 1
        assert len(usage.calls) == 1
        assert usage.calls[0]["model_id"] == model_id
        assert usage.calls[0]["status"] == "complete"
        assert usage.calls[0]["target"].provider == "speech_voice_live"
        assert usage.calls[0]["target"].target == "managed_voice_live"
        assert usage.calls[0]["target"].region == "eastus2"
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize(
    "query",
    [
        "model=wrong",
        "model=GPT-REALTIME",
        "model=gpt-realtime-preview",
        "model=gpt-4.1-preview",
        "model=gpt-realtime&region=westus",
        "model=gpt-realtime&region=EastUS2",
    ],
)
def test_live_speech_rejects_nonmatching_model_or_region(query):
    c = _speech_client()
    try:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(
                f"/api/voice/live?provider=speech_voice_live&{query}",
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
        assert "endpointPath" not in providers["azure_openai"]
        assert "modelCatalogRef" not in providers["azure_openai"]
        assert "endpointPath" not in providers["speech_voice_live"]
        assert "modelCatalogRef" not in providers["speech_voice_live"]
        assert providers["speech_voice_live"]["defaultManagedModelId"] == "gpt-realtime"
        assert [model["id"] for model in providers["speech_voice_live"]["managedModels"]] == [
            "gpt-realtime",
            "gpt-realtime-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-5-mini",
            "gpt-5.1",
        ]
        assert providers["speech_voice_live"]["capabilities"]["voices"]["kind"] == "azure-standard"
        assert "inputTranscription" not in providers["speech_voice_live"]["capabilities"]
        assert "inputTranscription" not in providers["speech_voice_live"]["sessionDefaults"]
        for model in providers["speech_voice_live"]["managedModels"]:
            assert set(model) == {
                "id",
                "displayName",
                "description",
                "profile",
                "inputTranscription",
                "apiVersion",
                "initialRegion",
                "audioFormat",
                "sampleRateHz",
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


def test_live_speech_client_disconnect_records_cancelled_managed_voice_usage(caplog):
    caplog.set_level("INFO", logger="ai4ia_api.routers.realtime")
    c = _speech_client()
    capture = _attach_completion_capture(caplog)
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
        assert call["status"] == "cancelled"
        assert call["usage"].known is False
        assert call["usage"].complete is False
        assert call["usage"].calls == 1
        target = call["target"]
        assert target.provider == "speech_voice_live"
        assert target.deployment is None
        assert target.target == "managed_voice_live"
        assert target.region == "eastus2"
        payloads = _completion_payloads(caplog)
        assert len(payloads) == 1
        assert payloads[0]["outcome"] == "cancelled"
        assert payloads[0]["metadata"]["sourceEvent"] in {
            "websocket.disconnect",
            "framework.cancelled",
        }
    finally:
        if capture is not None:
            capture.removeHandler(caplog.handler)
        c.__exit__(None, None, None)


def test_live_normal_upstream_close_records_complete_once_and_logs_stats(caplog):
    caplog.set_level("INFO", logger="ai4ia_api.routers.realtime")
    c = _client(realtime_enabled=True)
    capture = _attach_completion_capture(caplog)
    try:
        connector = ScriptedRealtimeConnector(
            [
                UpstreamMessage(
                    "text",
                    text='{"type":"response.done","transcript":"private"}',
                    source_event="TEXT",
                ),
                UpstreamMessage("binary", data=b"private-audio", source_event="BINARY"),
                UpstreamMessage("close", close_code=1000, source_event="CLOSE"),
            ]
        )
        usage = FakeUsageService()
        c.app.state.realtime_connector = connector
        c.app.state.usage = usage

        with c.websocket_connect(
            "/api/voice/live",
            subprotocols=[DEV_SUBPROTOCOL, "completeuser"],
            headers=_origin(),
        ) as ws:
            assert json.loads(ws.receive_text())["type"] == "response.done"
            assert ws.receive_bytes() == b"private-audio"
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()

        assert exc.value.code == 1000
        assert connector.upstream.close_calls == 1
        assert len(usage.calls) == 1
        assert usage.calls[0]["status"] == "complete"
        payloads = _completion_payloads(caplog)
        assert len(payloads) == 1
        payload = payloads[0]
        assert payload["outcome"] == "complete"
        assert payload["metadata"]["closeCode"] == 1000
        assert payload["stats"]["upstreamToClient"]["textFrames"] == 1
        assert payload["stats"]["upstreamToClient"]["binaryFrames"] == 1
        assert payload["stats"]["upstreamToClient"]["eventTypes"] == ["response.done"]
        encoded = json.dumps(payload)
        assert "private-audio" not in encoded
        assert "private" not in encoded
        assert "completeuser" not in encoded
    finally:
        if capture is not None:
            capture.removeHandler(caplog.handler)
        c.__exit__(None, None, None)


def test_live_usage_failure_does_not_change_complete_outcome(caplog):
    caplog.set_level("INFO", logger="ai4ia_api.routers.realtime")
    c = _client(realtime_enabled=True)
    capture = _attach_completion_capture(caplog)
    try:
        connector = ScriptedRealtimeConnector(
            [UpstreamMessage("close", close_code=1000, source_event="CLOSE")]
        )
        usage = FailingUsageService()
        c.app.state.realtime_connector = connector
        c.app.state.usage = usage

        with c.websocket_connect(
            "/api/voice/live",
            subprotocols=[DEV_SUBPROTOCOL, "usagefailure"],
            headers=_origin(),
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()

        assert exc.value.code == 1000
        assert len(usage.calls) == 1
        assert usage.calls[0]["status"] == "complete"
        payloads = _completion_payloads(caplog)
        assert len(payloads) == 1
        assert payloads[0]["outcome"] == "complete"
        assert payloads[0]["usageError"] == {
            "exceptionClass": "RuntimeError",
            "exceptionMessage": "api_key=[REDACTED]",
        }
        assert "metering-secret" not in json.dumps(payloads[0])
    finally:
        if capture is not None:
            capture.removeHandler(caplog.handler)
        c.__exit__(None, None, None)


def test_live_protocol_error_then_close_records_error_and_logs_only_safe_fields(caplog):
    caplog.set_level("INFO", logger="ai4ia_api.routers.realtime")
    raw = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "code": "bad_request",
                "param": "session.voice",
                "event_id": "evt-safe",
                "message": "Bearer protocol-secret api_key=second-secret",
            },
            "audio": "private-base64",
            "instructions": "private prompt",
        }
    )
    c = _speech_client()
    capture = _attach_completion_capture(caplog)
    try:
        connector = ScriptedRealtimeConnector(
            [
                UpstreamMessage("text", text=raw, source_event="TEXT"),
                UpstreamMessage(
                    "close",
                    close_code=1000,
                    close_reason="token=close-secret",
                    source_event="CLOSE",
                ),
            ]
        )
        usage = FakeUsageService()
        c.app.state.realtime_connector = connector
        c.app.state.usage = usage

        with c.websocket_connect(
            "/api/voice/live?provider=speech_voice_live",
            subprotocols=[DEV_SUBPROTOCOL, "protocoluser"],
            headers=_origin(),
        ) as ws:
            assert ws.receive_text() == raw
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()

        assert exc.value.code == 1011
        assert len(usage.calls) == 1
        assert usage.calls[0]["status"] == "error"
        payloads = _completion_payloads(caplog)
        assert len(payloads) == 1
        payload = payloads[0]
        assert payload["outcome"] == "error"
        assert payload["metadata"]["closeCode"] == 1000
        assert payload["metadata"]["closeReason"] == "token=[REDACTED]"
        assert payload["metadata"]["protocolError"] == {
            "type": "invalid_request_error",
            "code": "bad_request",
            "param": "session.voice",
            "event_id": "evt-safe",
            "message": "Bearer [REDACTED] api_key=[REDACTED]",
        }
        encoded = json.dumps(payload)
        for forbidden in (
            "protocol-secret",
            "second-secret",
            "close-secret",
            "private-base64",
            "private prompt",
            "protocoluser",
        ):
            assert forbidden not in encoded
    finally:
        if capture is not None:
            capture.removeHandler(caplog.handler)
        c.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("message", "expected_source", "expected_code"),
    [
        (
            UpstreamMessage(
                "error",
                exception_class="RuntimeError",
                exception_message="Authorization: Bearer upstream-secret",
                source_event="ERROR",
            ),
            "ERROR",
            None,
        ),
        (
            UpstreamMessage(
                "close",
                close_code=1013,
                close_reason="api_key=close-secret",
                source_event="CLOSE",
            ),
            "CLOSE",
            1013,
        ),
    ],
)
def test_live_upstream_error_or_abnormal_close_logs_safely(
    caplog, message, expected_source, expected_code
):
    caplog.set_level("INFO", logger="ai4ia_api.routers.realtime")
    c = _client(realtime_enabled=True)
    capture = _attach_completion_capture(caplog)
    try:
        connector = ScriptedRealtimeConnector([message])
        usage = FakeUsageService()
        c.app.state.realtime_connector = connector
        c.app.state.usage = usage

        with c.websocket_connect(
            "/api/voice/live",
            subprotocols=[DEV_SUBPROTOCOL, "erroruser"],
            headers=_origin(),
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()

        assert exc.value.code == 1011
        assert len(usage.calls) == 1
        assert usage.calls[0]["status"] == "error"
        payloads = _completion_payloads(caplog)
        assert len(payloads) == 1
        payload = payloads[0]
        assert payload["outcome"] == "error"
        assert payload["metadata"]["sourceEvent"] == expected_source
        assert payload["metadata"]["closeCode"] == expected_code
        encoded = json.dumps(payload)
        assert "upstream-secret" not in encoded
        assert "close-secret" not in encoded
        assert "erroruser" not in encoded
    finally:
        if capture is not None:
            capture.removeHandler(caplog.handler)
        c.__exit__(None, None, None)


def test_live_max_duration_records_cancelled_once(caplog):
    caplog.set_level("INFO", logger="ai4ia_api.routers.realtime")
    c = _client(realtime_enabled=True, realtime_max_session_seconds=0.01)
    capture = _attach_completion_capture(caplog)
    try:
        usage = FakeUsageService()
        c.app.state.usage = usage
        with c.websocket_connect(
            "/api/voice/live",
            subprotocols=[DEV_SUBPROTOCOL, "timeoutuser"],
            headers=_origin(),
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()

        assert exc.value.code == 1000
        assert len(usage.calls) == 1
        assert usage.calls[0]["status"] == "cancelled"
        payloads = _completion_payloads(caplog)
        assert len(payloads) == 1
        assert payloads[0]["outcome"] == "cancelled"
        assert payloads[0]["metadata"]["sourceEvent"] == "max_duration_timeout"
    finally:
        if capture is not None:
            capture.removeHandler(caplog.handler)
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
