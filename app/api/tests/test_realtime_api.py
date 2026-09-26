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
import time
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ai4ia_api import realtime_avatar
from ai4ia_api.hard_quota.dispatch import DispatchLease
from ai4ia_api.hard_quota.models import QuotaError
from ai4ia_api.main import create_app
from ai4ia_api.photo_avatars.live import LiveAvatarError, LiveAvatarGrant
from ai4ia_api.policy.models import PolicyDecision, PolicyError
from ai4ia_api.realtime_avatar import AVATAR_VIDEO_FRAME_MAX_CHARS
from ai4ia_api.routers import realtime as realtime_module
from ai4ia_api.routers.realtime import (
    DEV_SUBPROTOCOL,
    UpstreamMessage,
)
from ai4ia_api.usage.pricing import PricingBook
from tests.conftest import make_settings
from tests.test_photo_avatar_live import OTHER_RECORD_ID as AVATAR_OTHER_RECORD_ID
from tests.test_photo_avatar_live import PROVIDER_ID as AVATAR_PROVIDER_ID
from tests.test_photo_avatar_live import RECORD_ID as AVATAR_RECORD_ID
from tests.test_photo_avatar_live import Rig as AvatarRig

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


def test_live_origin_rejected_when_allowlist_set(monkeypatch):
    events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.realtime.emit_security_block",
        lambda category, reason, source: events.append((category, reason, source)),
    )
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
    assert events == [("realtime_auth", "origin_rejected", "voice_live")]


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


# --------------------------------------------------------------------------- #
# Live photo avatars (Phase 2) through the real route, with layer 1's service.
# --------------------------------------------------------------------------- #

AVATAR_QUERY = f"?provider=speech_voice_live&avatar={AVATAR_RECORD_ID}"


def _avatar_client(**overrides) -> tuple[TestClient, AvatarRig]:
    c = _speech_client(photo_avatars_enabled=True, **overrides)
    original = c.app.state.photo_avatars
    if original is not None:
        c.portal.call(original.close)
    rig = AvatarRig(policy=c.app.state.policy)
    c.app.state.photo_avatars = rig.service
    return c, rig


def _seed_avatar(c: TestClient, rig: AvatarRig, user: str = "alice", **fields) -> str:
    owner = _internal_id(c, {"X-Dev-User": user})
    c.portal.call(lambda: rig.seed(owner, **fields))
    return owner


def _avatar_video(total_chars: int, fill: str = "A") -> str:
    prefix, suffix = '{"type":"response.video.delta","codec":"h264","delta":"', '"}'
    return prefix + fill * (total_chars - len(prefix) - len(suffix)) + suffix


def _avatar_refusal(c: TestClient, record_id: str = AVATAR_RECORD_ID, user: str = "alice") -> dict:
    with c.websocket_connect(
        f"/api/voice/live?provider=speech_voice_live&avatar={record_id}",
        subprotocols=[DEV_SUBPROTOCOL, user], headers=_origin(),
    ) as ws:
        error = json.loads(ws.receive_text())["error"]
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1008
    assert error["type"] == "avatar_error"
    return error


def _avatar_connects(c: TestClient, record_id: str = AVATAR_RECORD_ID, user: str = "alice") -> dict:
    with c.websocket_connect(
        f"/api/voice/live?provider=speech_voice_live&avatar={record_id}",
        subprotocols=[DEV_SUBPROTOCOL, user], headers=_origin(),
    ) as ws:
        return json.loads(ws.receive_text())


class _ClockedUpstream(ScriptedUpstream):
    """Scripted frames that each set the avatar meter's clock as they arrive."""

    def __init__(self, messages: list[UpstreamMessage], clock, times: list[float]) -> None:
        super().__init__(messages)
        self.clock = clock
        self.times = list(times)

    async def receive(self) -> UpstreamMessage:
        message = await super().receive()
        if self.times:
            self.clock.now = self.times.pop(0)
        return message


class _ClockedConnector(ScriptedRealtimeConnector):
    def __init__(self, messages: list[UpstreamMessage], clock, times: list[float]) -> None:
        self.upstream = _ClockedUpstream(messages, clock, times)
        self.connects: list[dict] = []


class _Clock:
    def __init__(self, start: float = 50.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class PricedUsageService(FakeUsageService):
    def __init__(self, pricing: PricingBook) -> None:
        super().__init__()
        self.pricing = pricing


def test_live_avatar_injects_the_server_block_and_drops_client_avatar_fields():
    hostile = {"type": "session.update", "session": {
        "voice": {"type": "azure-standard", "name": "en-US-AvaNeural"},
        "avatar": {
            "type": "video-avatar", "character": "someone-else", "customized": False,
            "output_protocol": "webrtc", "output_audit_audio": True,
        },
    }}
    c, rig = _avatar_client()
    try:
        _seed_avatar(c, rig)
        connector = c.app.state.realtime_connector
        with c.websocket_connect(
            f"/api/voice/live{AVATAR_QUERY}", subprotocols=[DEV_SUBPROTOCOL, "alice"],
            headers=_origin(),
        ) as ws:
            assert json.loads(ws.receive_text()) == {
                "type": "ai4ia.avatar.session", "output_protocol": "websocket",
                "idle_timeout_seconds": 120, "idle_warning_seconds": 30,
                "max_session_seconds": 600,
            }
            ws.send_text(json.dumps(hostile))
            echoed = ws.receive_text()
        sent = json.loads(connector.upstream.sent_text[0])
        assert sent["session"]["avatar"] == {
            "type": "photo-avatar", "model": "vasa-1", "character": AVATAR_PROVIDER_ID,
            "customized": True, "output_protocol": "websocket",
        }
        assert sent["session"]["voice"]["name"] == "en-US-AvaNeural"
        for forbidden in ("someone-else", "webrtc", "output_audit_audio"):
            assert forbidden not in connector.upstream.sent_text[0]
        # Even an echo of the upstream frame reaches the browser without the id.
        assert AVATAR_PROVIDER_ID not in echoed and "[avatar]" in echoed
    finally:
        c.__exit__(None, None, None)

    control = _speech_client()
    try:
        with control.websocket_connect(
            "/api/voice/live?provider=speech_voice_live", subprotocols=[DEV_SUBPROTOCOL, "alice"],
            headers=_origin(),
        ) as ws:
            ws.send_text(json.dumps(hostile))
            ws.receive_text()
        plain = json.loads(control.app.state.realtime_connector.upstream.sent_text[0])
        assert "avatar" not in plain["session"]
    finally:
        control.__exit__(None, None, None)


@pytest.mark.parametrize("query", [AVATAR_QUERY, "?provider=speech_voice_live", ""])
def test_live_refuses_client_avatar_connect_while_other_frames_pass(query):
    append = '{"type":"input_audio_buffer.append","audio":"AAA="}'
    c, rig = _avatar_client()
    try:
        _seed_avatar(c, rig)
        connector = c.app.state.realtime_connector
        with c.websocket_connect(
            f"/api/voice/live{query}", subprotocols=[DEV_SUBPROTOCOL, "alice"], headers=_origin(),
        ) as ws:
            if "avatar=" in query:
                assert json.loads(ws.receive_text())["type"] == "ai4ia.avatar.session"
            ws.send_text(append)
            assert json.loads(ws.receive_text().removeprefix("echo:")) == json.loads(append)
            ws.send_text('{"type":"session.avatar.connect","client_sdp":"dj0wDQ=="}')
            refused = json.loads(ws.receive_text())
            assert refused["error"]["code"] == "avatar_connect_refused"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 1008
        assert [json.loads(frame) for frame in connector.upstream.sent_text] == [json.loads(append)]
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("guard", ["owner", "ready", "flag", "capability"])
def test_live_avatar_rechecks_the_grant_at_every_connect(guard):
    c, rig = _avatar_client()
    try:
        _seed_avatar(c, rig)
        connector = c.app.state.realtime_connector
        if guard == "owner":
            assert _avatar_refusal(c, user="bob")["reason"] == "not_found"
        elif guard == "ready":
            _seed_avatar(
                c, rig, record_id=AVATAR_OTHER_RECORD_ID, status="generating", preview=None,
                readyAt=None,
            )
            assert _avatar_refusal(c, AVATAR_OTHER_RECORD_ID)["reason"] == "not_ready"
        elif guard == "flag":
            c.app.state.settings.photo_avatars_enabled = False
            assert _avatar_refusal(c)["reason"] == "disabled"
            c.app.state.settings.photo_avatars_enabled = True
        else:
            # A first, granted connection proves nothing is cached for the next one.
            assert _avatar_connects(c)["type"] == "ai4ia.avatar.session"
            rig.provider.features = []
            rig.service._capability.invalidate()
            assert _avatar_refusal(c)["reason"] == "capability_unavailable"
            assert len(connector.connects) == 1
            return
        assert connector.connects == []
        assert _avatar_connects(c)["type"] == "ai4ia.avatar.session"
        assert len(connector.connects) == 1
    finally:
        c.__exit__(None, None, None)


def test_live_avatar_policy_denial_and_home_mismatch_open_no_upstream(monkeypatch):
    mode = {"refuse": True, "region": "swedencentral"}

    async def fake_resolve(state, user, record_id):
        if mode["refuse"]:
            raise LiveAvatarError(403, "policy_denied", "Not permitted.")
        return LiveAvatarGrant(
            record_id=record_id, provider_avatar_id=AVATAR_PROVIDER_ID, base_model="vasa-1",
            home_region=mode["region"],
        )

    monkeypatch.setattr(realtime_avatar, "resolve_live_avatar", fake_resolve)
    c, _ = _avatar_client()
    try:
        connector = c.app.state.realtime_connector
        assert _avatar_refusal(c)["reason"] == "policy_denied"
        mode["refuse"] = False
        assert _avatar_refusal(c)["reason"] == "home_mismatch"
        assert connector.connects == []
        mode["region"] = "eastus2"
        assert _avatar_connects(c)["type"] == "ai4ia.avatar.session"
        assert len(connector.connects) == 1
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("provider_id", ["not-an-issued-id", AVATAR_PROVIDER_ID])
def test_live_avatar_refuses_a_malformed_grant_from_the_resolver(monkeypatch, provider_id):
    async def fake_resolve(state, user, record_id):
        return LiveAvatarGrant(
            record_id=record_id, provider_avatar_id=provider_id, base_model="vasa-1",
            home_region="eastus2",
        )

    monkeypatch.setattr(realtime_avatar, "resolve_live_avatar", fake_resolve)
    c, _ = _avatar_client()
    try:
        connector = c.app.state.realtime_connector
        if provider_id == AVATAR_PROVIDER_ID:
            assert _avatar_connects(c)["type"] == "ai4ia.avatar.session"
            assert len(connector.connects) == 1
        else:
            assert _avatar_refusal(c)["reason"] == "unavailable"
            assert connector.connects == []
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("query", [
    f"?avatar={AVATAR_RECORD_ID}",
    f"?provider=azure_openai&avatar={AVATAR_RECORD_ID}",
    "?provider=speech_voice_live&avatar=ABC",
    "?provider=speech_voice_live&avatar=",
])
def test_live_avatar_needs_speech_and_a_record_id_before_accept(query):
    c, rig = _avatar_client()
    try:
        _seed_avatar(c, rig)
        connector = c.app.state.realtime_connector
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(
                f"/api/voice/live{query}", subprotocols=[DEV_SUBPROTOCOL, "alice"],
                headers=_origin(),
            ):
                pass
        assert connector.connects == []
        assert _avatar_connects(c)["type"] == "ai4ia.avatar.session"
    finally:
        c.__exit__(None, None, None)


def test_live_avatar_oversized_video_closes_while_normal_video_forwards():
    c, rig = _avatar_client()
    try:
        _seed_avatar(c, rig)
        video = _avatar_video(8_000)
        connector = ScriptedRealtimeConnector([
            UpstreamMessage("text", text=video),
            UpstreamMessage("text", text=_avatar_video(AVATAR_VIDEO_FRAME_MAX_CHARS + 1)),
        ])
        c.app.state.realtime_connector = connector
        c.app.state.usage = usage = FakeUsageService()
        with c.websocket_connect(
            f"/api/voice/live{AVATAR_QUERY}", subprotocols=[DEV_SUBPROTOCOL, "alice"],
            headers=_origin(),
        ) as ws:
            assert json.loads(ws.receive_text())["type"] == "ai4ia.avatar.session"
            assert ws.receive_text() == video
            assert json.loads(ws.receive_text())["error"]["code"] == "avatar_frame_too_large"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 1009
        assert connector.upstream.close_calls == 1
        meter = [call for call in usage.calls if call.get("billing_unit") == "second"]
        assert len(meter) == 1 and meter[0]["status"] == "error"
    finally:
        c.__exit__(None, None, None)


def test_live_avatar_frames_and_provider_id_never_reach_logs_telemetry_or_the_browser(
    caplog, monkeypatch,
):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        realtime_module, "emit_custom_event", lambda name, attrs: events.append((name, attrs)),
    )
    monkeypatch.setattr(
        "ai4ia_api.usage.service.emit_custom_event",
        lambda name, attrs: events.append((name, attrs)),
    )
    c, rig = _avatar_client()
    root = logging.getLogger()
    root.addHandler(caplog.handler)  # create_app reset the root handlers
    root.setLevel(logging.INFO)
    try:
        _seed_avatar(c, rig)
        sentinel = _avatar_video(4_096, fill="Z").replace("ZZZZ", "VIDEOSENTINEL", 1)
        frames = [
            json.dumps({"type": "session.updated", "session": {
                "modalities": ["audio", "text", "avatar"],
                "avatar": {
                    "type": "photo-avatar", "character": AVATAR_PROVIDER_ID,
                    "output_protocol": "websocket",
                    "ice_servers": [{"urls": ["turn:x"], "credential": "turn-secret"}],
                },
            }}),
            sentinel,
            json.dumps({"type": "error", "error": {
                "code": "rate_limited", "message": f"Avatar {AVATAR_PROVIDER_ID} is busy.",
            }}),
        ]
        c.app.state.realtime_connector = ScriptedRealtimeConnector([
            *[UpstreamMessage("text", text=frame) for frame in frames],
            UpstreamMessage(
                "close", close_code=4000, close_reason=f"avatar {AVATAR_PROVIDER_ID} unavailable",
            ),
        ])
        received: list[str] = []
        with c.websocket_connect(
            f"/api/voice/live{AVATAR_QUERY}", subprotocols=[DEV_SUBPROTOCOL, "alice"],
            headers=_origin(),
        ) as ws:
            for _ in range(4):
                received.append(ws.receive_text())
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
        assert received[2] == sentinel  # video does reach the browser, verbatim
        browser = "".join(received)
        assert AVATAR_PROVIDER_ID not in browser and "turn-secret" not in browser
        logged = caplog.text
        for forbidden in (AVATAR_PROVIDER_ID, "VIDEOSENTINEL", "turn-secret"):
            assert forbidden not in logged
        completions = [
            json.loads(record.getMessage()) for record in caplog.records
            if '"event":"voice_live_completion"' in record.getMessage()
        ]
        assert len(completions) == 1
        evidence = completions[0]["avatar"]
        assert evidence["recordRef"] == AVATAR_RECORD_ID[:8]
        assert evidence["videoFrames"] == 1 and evidence["confirmed"] is True
        assert "[avatar]" in completions[0]["metadata"]["protocolError"]["message"]
        assert completions[0]["metadata"]["closeReason"] == "avatar [avatar] unavailable"
        telemetry = json.dumps(events, default=str)
        for forbidden in (AVATAR_PROVIDER_ID, "VIDEOSENTINEL", "turn-secret"):
            assert forbidden not in telemetry
        live = [attrs for name, attrs in events if name == "voice_live_completion"]
        assert live and live[0]["avatarRef"] == AVATAR_RECORD_ID[:8]
        metered = [attrs for name, attrs in events if name == "chat_completion"]
        assert any(attrs.get("resourceRef") == AVATAR_RECORD_ID[:8] for attrs in metered)
    finally:
        root.removeHandler(caplog.handler)
        c.__exit__(None, None, None)


@pytest.mark.parametrize("confirmed", [True, False])
def test_live_avatar_meter_records_confirmed_seconds_only(monkeypatch, confirmed):
    clock = _Clock()
    monkeypatch.setattr(realtime_avatar, "monotonic", clock)
    first = (
        json.dumps({"type": "session.updated", "session": {"modalities": ["audio", "text", "avatar"]}})
        if confirmed else '{"type":"response.audio_transcript.delta","delta":"Hi"}'
    )
    c, rig = _avatar_client()
    try:
        _seed_avatar(c, rig)
        c.app.state.realtime_connector = _ClockedConnector(
            [
                UpstreamMessage("text", text=first),
                UpstreamMessage("text", text='{"type":"response.done"}'),
                UpstreamMessage("close", close_code=1000),
            ],
            clock, [100.0, 101.0, 104.2],
        )
        c.app.state.usage = usage = FakeUsageService()
        with c.websocket_connect(
            f"/api/voice/live{AVATAR_QUERY}", subprotocols=[DEV_SUBPROTOCOL, "alice"],
            headers=_origin(),
        ) as ws:
            for _ in range(3):
                ws.receive_text()
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
        voice = [call for call in usage.calls if call.get("billing_unit") is None]
        meter = [call for call in usage.calls if call.get("billing_unit") == "second"]
        assert len(voice) == 1 and voice[0]["model_id"] == "gpt-realtime"
        if not confirmed:
            assert meter == []
            return
        assert len(meter) == 1
        row = meter[0]
        assert row["billable_units"] == 5  # ceil(104.2 - 100.0)
        assert row["model_id"] == "photo-avatar-realtime-standard"
        assert row["target"].provider == "azure_speech_photo_avatar"
        assert row["target"].target == "photo_avatar_live"
        assert row["target"].region == "eastus2"
        assert row["resource_ref"] == AVATAR_RECORD_ID[:8]
        assert row["provider_completed"] is True and row["status"] == "complete"
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize(("priced", "capped", "allowed"), [
    (False, True, False),
    (True, True, True),
    (False, False, True),
])
def test_live_avatar_unpriced_meter_refuses_only_under_a_cost_cap(monkeypatch, priced, capped, allowed):
    asked: list[str] = []

    async def fake_capped(state, user):
        asked.append(user.internal_user_id)
        return capped

    monkeypatch.setattr(realtime_avatar, "live_cost_capped", fake_capped)
    c, rig = _avatar_client()
    try:
        _seed_avatar(c, rig)
        if not priced:
            c.app.state.usage = PricedUsageService(PricingBook({}, currency="USD", version="none"))
        connector = c.app.state.realtime_connector
        if allowed:
            assert _avatar_connects(c)["type"] == "ai4ia.avatar.session"
            assert len(connector.connects) == 1
        else:
            error = _avatar_refusal(c)
            assert error["code"] == "cost_unknown_under_cap"
            assert connector.connects == []
        # A known price needs no cap lookup; an unknown one always asks.
        assert len(asked) == (0 if priced else 1)
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("talking", [False, True])
def test_live_avatar_idle_timeout_ends_only_a_silent_session(monkeypatch, talking):
    monkeypatch.setattr(realtime_avatar, "IDLE_TICK_SECONDS", 0.02)
    monkeypatch.setattr(realtime_avatar, "idle_warning_seconds", lambda timeout: 0.2)
    c, rig = _avatar_client()
    try:
        _seed_avatar(c, rig)
        c.app.state.settings.photo_avatar_live_idle_timeout_seconds = 0.6
        received: list[dict] = []
        with c.websocket_connect(
            f"/api/voice/live{AVATAR_QUERY}", subprotocols=[DEV_SUBPROTOCOL, "alice"],
            headers=_origin(),
        ) as ws:
            assert json.loads(ws.receive_text())["type"] == "ai4ia.avatar.session"
            if talking:
                deadline = time.monotonic() + 1.2
                while time.monotonic() < deadline:
                    ws.send_text(
                        '{"type":"conversation.item.create","item":{"type":"message",'
                        '"role":"user","content":[{"type":"input_text","text":"still here"}]}}'
                    )
                    frame = ws.receive_text()
                    if not frame.startswith("echo:"):
                        received.append(json.loads(frame))
                    time.sleep(0.05)
            else:
                received.append(json.loads(ws.receive_text()))
                received.append(json.loads(ws.receive_text()))
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_text()
        if talking:
            assert received == []  # no warning and no end while the user talks
        else:
            assert received[0]["type"] == "ai4ia.avatar.idle_warning"
            assert received[1] == {"type": "ai4ia.avatar.session_ended", "reason": "idle_timeout"}
            assert closed.value.code == 1000
    finally:
        c.__exit__(None, None, None)


def test_live_avatar_session_cap_ends_with_a_notice():
    c, rig = _avatar_client(realtime_max_session_seconds=0.1)
    try:
        _seed_avatar(c, rig)
        with c.websocket_connect(
            f"/api/voice/live{AVATAR_QUERY}", subprotocols=[DEV_SUBPROTOCOL, "alice"],
            headers=_origin(),
        ) as ws:
            session = json.loads(ws.receive_text())
            assert session["max_session_seconds"] == 1
            assert json.loads(ws.receive_text()) == {
                "type": "ai4ia.avatar.session_ended", "reason": "session_limit",
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 1000
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("code", ["avatar_verification_failed", "rate_limited"])
def test_live_avatar_verification_failure_is_stable_and_marks_once(code):
    c, rig = _avatar_client()
    try:
        owner = _seed_avatar(c, rig)
        failure = json.dumps({"type": "error", "error": {
            "type": "invalid_request_error", "code": code,
            "message": f"Avatar {AVATAR_PROVIDER_ID} failed verification.",
        }})
        c.app.state.realtime_connector = ScriptedRealtimeConnector([
            UpstreamMessage("text", text=failure),
            UpstreamMessage("close", close_code=1000),
        ])
        with c.websocket_connect(
            f"/api/voice/live{AVATAR_QUERY}", subprotocols=[DEV_SUBPROTOCOL, "alice"],
            headers=_origin(),
        ) as ws:
            assert json.loads(ws.receive_text())["type"] == "ai4ia.avatar.session"
            error = json.loads(ws.receive_text())["error"]
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 1011
        stored = c.portal.call(lambda: rig.stored(owner))
        if code == "avatar_verification_failed":
            assert error == json.loads(realtime_avatar.unavailable_error("verification_failed"))["error"]
            assert stored.liveVerificationFailedAt is not None
            # Layer 1's cooldown now refuses the next session before any upstream.
            again = _avatar_refusal(c)
            assert again["reason"] == "needs_reverification" and again["retry_after_seconds"] >= 1
        else:
            assert error["code"] == "rate_limited"
            assert AVATAR_PROVIDER_ID not in json.dumps(error)
            assert stored.liveVerificationFailedAt is None
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("refuse", [True, False])
def test_live_avatar_admission_runs_first_on_its_own_surface(monkeypatch, refuse):
    calls: list[tuple[str, dict]] = []

    @asynccontextmanager
    async def recording(surface, payload, **kwargs):
        calls.append((surface, payload))
        if refuse and surface == "avatar_live":
            raise QuotaError("Hard quota requestsPerMinute would be exceeded.", code=429)
        yield DispatchLease()

    monkeypatch.setattr(realtime_module, "admitted_dispatch", recording)
    c, rig = _avatar_client()
    try:
        _seed_avatar(c, rig)
        connector = c.app.state.realtime_connector
        with c.websocket_connect(
            f"/api/voice/live{AVATAR_QUERY}", subprotocols=[DEV_SUBPROTOCOL, "alice"],
            headers=_origin(),
        ) as ws:
            if refuse:
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_text()
                assert closed.value.code == 1011
            else:
                assert json.loads(ws.receive_text())["type"] == "ai4ia.avatar.session"
        surfaces = [surface for surface, _ in calls]
        assert surfaces == (["avatar_live"] if refuse else ["avatar_live", "realtime"])
        payload = calls[0][1]
        assert payload["operation"] == "live_session" and payload["avatar"] == AVATAR_RECORD_ID[:8]
        assert AVATAR_PROVIDER_ID not in json.dumps(payload)
        assert len(connector.connects) == (0 if refuse else 1)
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("revoke", [True, False])
def test_live_avatar_use_is_rechecked_on_every_upstream_send(monkeypatch, revoke):
    revoked = {"on": False}
    real_require = realtime_module.require_policy

    async def guarded(request, **kwargs):
        if request.operation == "avatar.use" and revoked["on"]:
            raise PolicyError(PolicyDecision("deny", "policy_denied"))
        await real_require(request, **kwargs)

    monkeypatch.setattr(realtime_module, "require_policy", guarded)
    append = '{"type":"input_audio_buffer.append","audio":"AAA="}'
    c, rig = _avatar_client()
    try:
        _seed_avatar(c, rig)
        connector = c.app.state.realtime_connector
        with c.websocket_connect(
            f"/api/voice/live{AVATAR_QUERY}", subprotocols=[DEV_SUBPROTOCOL, "alice"],
            headers=_origin(),
        ) as ws:
            ws.receive_text()
            ws.send_text(append)
            ws.receive_text()
            revoked["on"] = revoke
            # Stopping the avatar's speech never needs a fresh grant.
            ws.send_text('{"type":"output_audio_buffer.clear"}')
            assert json.loads(ws.receive_text().removeprefix("echo:")) == {"type": "output_audio_buffer.clear"}
            ws.send_text(append)
            if revoke:
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_text()
                assert closed.value.code == 1011
            else:
                ws.receive_text()
        assert len(connector.upstream.sent_text) == (2 if revoke else 3)
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("bound", [True, False])
def test_live_avatar_receipt_is_written_for_chat_bound_sessions_only(bound):
    c, rig = _avatar_client()
    try:
        _seed_avatar(c, rig)
        created = c.post(
            "/api/sessions", headers={"X-Dev-User": "alice"}, json={"title": "Avatar chat"},
        )
        assert created.status_code == 201
        session_id = created.json()["id"]
        c.app.state.realtime_connector = ScriptedRealtimeConnector([
            UpstreamMessage("text", text=json.dumps({"type": "session.updated", "session": {
                "modalities": ["audio", "text", "avatar"],
            }})),
            UpstreamMessage("text", text=_avatar_video(2_048)),
            UpstreamMessage("close", close_code=1000),
        ])
        query = AVATAR_QUERY + (f"&session={session_id}" if bound else "")
        with c.websocket_connect(
            f"/api/voice/live{query}", subprotocols=[DEV_SUBPROTOCOL, "alice"], headers=_origin(),
        ) as ws:
            for _ in range(3):
                ws.receive_text()
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
        messages = c.get(
            f"/api/sessions/{session_id}/messages", headers={"X-Dev-User": "alice"},
        ).json()
        ended = [m for m in messages if m["content"] == "Avatar voice session ended."]
        if not bound:
            assert ended == []
            return
        assert len(ended) == 1
        receipt = ended[0]["executionReceipt"]
        assert receipt["avatar"]["recordRef"] == AVATAR_RECORD_ID[:8]
        assert receipt["avatar"]["confirmed"] is True and receipt["avatar"]["billableSeconds"] >= 1
        assert receipt["avatar"]["videoFrames"] == 1
        assert receipt["avatar"]["cost"]["known"] is True
        assert receipt["avatar"]["cost"]["billingModelId"] == "photo-avatar-realtime-standard"
        assert "avatar_media_not_recorded" in receipt["notes"]
        assert receipt["runtime"]["api"] == "speech"
        assert AVATAR_PROVIDER_ID not in json.dumps(ended[0])
    finally:
        c.__exit__(None, None, None)
