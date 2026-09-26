"""Merged policy/publication/canary/telemetry seams with synthetic transports only."""
from __future__ import annotations

import asyncio
import io
import json
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from ai4ia_api.catalog import ModelCatalog, _transform_infra_models, load_catalog
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.genai_telemetry import INSTRUMENTATION_NAME
from ai4ia_api.policy.context import bind_authenticated, clear_policy_context
from ai4ia_api.publishing.refs import AssetVersionRef
from ai4ia_api.realtime_canary import SETUP_INPUT
from ai4ia_api.realtime_protocol import RealtimeProtocol
from ai4ia_api.routers.realtime import BEARER_SUBPROTOCOL, RealtimeResolutionError, resolve_realtime_deployment
from ai4ia_api.usage.pricing import load_pricing
from tests.test_ga_voice_migration import GA_MODELS
from tests.test_genai_telemetry import _gateway, _model, _native, capture as capture
from tests.test_group_policy import service, user
from tests.test_model_catalog_portability import _load_gen
from tests.test_publication_execution_api import publish, published_api as published_api
from tests.test_realtime_api import FakeRealtimeConnector
from tests.test_realtime_canary_integration import (
    ACTOR, MONITOR, ORIGIN, setup_client as setup_client, setup_target,
)
from tests.test_realtime_staged_api import GA_SETTINGS

ROOT = Path(__file__).resolve().parents[3]
GA_MODEL = "gpt-realtime-1.5"
TTS_MODEL = "gpt-4o-mini-tts"


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\1\0" * 2400)
    return buffer.getvalue()


def test_declared_versions_and_runtime_flags_agree_in_all_merged_catalogs():
    source = json.loads((ROOT / "infra/models.json").read_text(encoding="utf-8"))
    catalogs = [
        load_catalog(),
        ModelCatalog.model_validate(_load_gen().build_catalog(source)),
        ModelCatalog.model_validate(_transform_infra_models(source)),
    ]
    for catalog in catalogs:
        assert len(catalog.models) == len(source["catalog"])
        for declaration in source["catalog"]:
            entry = next(m for m in catalog.models if m.id == declaration["name"])
            assert entry.runtimeEnabled is declaration.get("runtimeEnabled", True)
            assert entry.requiredRealtimeProtocol == declaration.get("requiredRealtimeProtocol")
            assert {(o.region, o.sku, o.modelVersion) for o in entry.options} == {
                (d["region"], d["sku"], d["version"]) for d in declaration["deployments"]
            }
        retained = catalog.get("gpt-realtime-2")
        assert retained.runtimeEnabled is False and retained.requiredRealtimeProtocol is None
        assert [o.modelVersion for o in retained.options] == ["2026-05-06"]
        assert catalog.resolve_deployment(retained.id) is None
        assert catalog.resolve_deployment(TTS_MODEL).modelVersion == "2025-12-15"
        for name, version in GA_MODELS.items():
            assert catalog.resolve_deployment(name).modelVersion == version


@pytest.mark.parametrize("model_id", GA_MODELS)
def test_actor_policy_bypass_never_bypasses_runtime_or_residency_filters(model_id):
    entry = load_catalog().get(model_id).model_copy(deep=True)
    catalog = ModelCatalog(models=[entry])
    policy, _ = service({"domains": {"models": {
        "default": {"allow": []},
        "mappings": [{"claim": "roles", "value": "Voice", "allow": ["realtime"]}],
    }}})
    policy.catalog = catalog
    try:
        bind_authenticated(policy, user())
        assert catalog.eligible_options(entry) == []
        # Publication compilation may ignore actor filtering, but nothing else.
        assert catalog.eligible_options(entry, policy_filter=False) == entry.options
        bind_authenticated(policy, user(roles=["Voice"]))
        assert catalog.eligible_options(entry) == entry.options
        with pytest.raises(RealtimeResolutionError, match="requires the GA protocol"):
            resolve_realtime_deployment(catalog, model_id, None)
        assert resolve_realtime_deployment(
            catalog, model_id, None, protocol=RealtimeProtocol.ga,
        )[0] == model_id
        entry.runtimeEnabled = False
        for policy_filter in (False, True):
            assert catalog.eligible_options(entry, policy_filter=policy_filter) == []
            assert catalog.resolve_deployment(model_id, policy_filter=policy_filter) is None
        entry.runtimeEnabled = True
        catalog.residencyPolicy = "us"
        assert catalog.eligible_options(entry, policy_filter=False) == []
        catalog.residencyPolicy = "global"
        assert catalog.eligible_options(entry, policy_filter=False) == entry.options
    finally:
        clear_policy_context()


@pytest.mark.parametrize("change", ["runtime_disabled", "declared_version", "required_protocol"])
@pytest.mark.parametrize("model_id", GA_MODELS)
def test_published_ga_voice_rechecks_catalog_binding_before_the_next_frame(published_api, change, model_id):
    client, _, headers, _ = published_api
    state = client.app.state
    state.settings.realtime_enabled = True
    state.settings.realtime_base_url = "https://realtime-gateway.test/openai"
    state.settings.realtime_gateway_api_key = "synthetic-preview-key"
    state.settings.realtime_protocol = RealtimeProtocol.ga
    for key, value in GA_SETTINGS.items():
        setattr(state.settings, key, value)
    head, source = publish(
        client, model_id, headers, "agent", "published-ga", tools=False, modes=["voice"],
    )
    _, version = asyncio.run(state.publications._version(AssetVersionRef.model_validate(source)))
    binding = next(item for item in version.modelBindings if item.modelId == model_id)
    assert binding.runtimeEnabled is True
    assert binding.requiredRealtimeProtocol == "ga"
    assert binding.option.modelVersion == GA_MODELS[model_id]
    auth = headers("Consumer")
    created = client.post("/api/sessions", headers=auth, json={
        "model": model_id, "agentName": head["handle"], "libraryDocumentIds": [],
    })
    assert created.status_code == 201, created.text
    query = f"/api/voice/live?session={created.json()['id']}&model={model_id}"
    protocols = [BEARER_SUBPROTOCOL, auth["Authorization"].split(" ", 1)[1]]
    original = state.catalog.get(model_id)
    connector = FakeRealtimeConnector()
    state.realtime_connector = connector
    frame = '{"type":"session.update","session":{"voice":"alloy"}}'
    with client.websocket_connect(
        query, headers={"origin": "http://localhost:3000"}, subprotocols=protocols,
    ) as ws:
        assert dict(ws.extra_headers)[b"x-ai4ia-realtime-protocol"] == b"ga"
        ws.send_text(frame)
        assert json.loads(ws.receive_text().removeprefix("echo:"))["session"]["type"] == "realtime"
        assert len(connector.upstream.sent_text) == 1
        if change == "runtime_disabled":
            updated = original.model_copy(update={"runtimeEnabled": False})
        elif change == "declared_version":
            updated = original.model_copy(update={
                "options": [o.model_copy(update={"modelVersion": "changed"}) for o in original.options],
            })
        else:
            updated = original.model_copy(update={"requiredRealtimeProtocol": None})
        state.catalog.models = [updated if m.id == model_id else m for m in state.catalog.models]
        ws.send_text(frame)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert len(connector.connects) == 1
    assert len(connector.upstream.sent_text) == 1
    state.catalog.models = [original if m.id == model_id else m for m in state.catalog.models]
    restored = FakeRealtimeConnector()
    state.realtime_connector = restored
    with client.websocket_connect(
        query, headers={"origin": "http://localhost:3000"}, subprotocols=protocols,
    ) as ws:
        ws.send_text(frame)
        assert json.loads(ws.receive_text().removeprefix("echo:"))["session"]["type"] == "realtime"
    assert len(restored.connects) == len(restored.upstream.sent_text) == 1


@pytest.mark.parametrize("actor", [ACTOR, MONITOR])
def test_rest_speech_cannot_reuse_a_restricted_monitor_actor(setup_client, actor):
    client, token, _, config = setup_client
    config["domains"]["models"]["default"]["allow"] = ["tts", "realtime"]
    client.app.state.settings.group_policy_json = json.dumps(config)
    calls = []
    audio = _wav()
    def response(request):
        calls.append(request)
        return httpx.Response(200, content=audio, headers={"Content-Type": "audio/wav"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(response))
    client.app.state.gateway = ModelGatewayClient(client.app.state.settings, http)
    body = {"model": TTS_MODEL, "input": "Synthetic speech.", "voice": "alloy", "format": "wav"}
    auth = {"Authorization": f"Bearer {token(actor)}"}
    try:
        denied = client.post("/api/voice/speech", json=body, headers=auth)
        assert denied.status_code == 403, denied.text
        assert calls == []
        # Same request and category grants, changing only whether the verified
        # identity belongs to a restricted profile. Never repurpose a monitor.
        ordinary = {"Authorization": f"Bearer {token('00000000-0000-0000-0000-000000000009')}"}
        allowed = client.post("/api/voice/speech", json=body, headers=ordinary)
        assert allowed.status_code == 200, allowed.text
        assert allowed.content == audio
        assert len(calls) == 1
    finally:
        asyncio.run(http.aclose())


def test_ga_setup_candidate_skips_runtime_disabled_rt2(setup_client):
    client, token, connector, _ = setup_client
    state = client.app.state
    catalog = state.catalog.model_copy(deep=True)
    state.catalog = state.policy.catalog = catalog
    bearer = token()
    assert "model=gpt-realtime&" in setup_target(client, bearer)
    default = catalog.get("gpt-realtime")
    retained = catalog.get("gpt-realtime-2")
    assert retained.runtimeEnabled is False
    default.runtimeEnabled = False
    target = setup_target(client, bearer)
    assert f"model={GA_MODEL}&" in target
    with client.websocket_connect(
        target, headers={"origin": ORIGIN}, subprotocols=[BEARER_SUBPROTOCOL, bearer],
    ) as ws:
        assert json.loads(ws.receive_text())["type"] == "session.created"
        ws.send_text(SETUP_INPUT)
        assert json.loads(ws.receive_text())["type"] == "session.updated"
    assert len(connector.calls) == 1
    url = urlsplit(connector.calls[0]["url"])
    assert url.path == "/openai/v1/realtime"
    assert parse_qs(url.query) == {"model": [catalog.resolve_deployment(GA_MODEL).deploymentName]}
    assert connector.upstream.sent_bytes == []
    assert len(connector.upstream.sent_text) == 1
    # Control: re-enabling only RT2 makes it the next candidate again.
    retained.runtimeEnabled = True
    assert f"model={retained.id}&" in setup_target(client, bearer)
    retained.runtimeEnabled = False
    default.runtimeEnabled = True
    assert "model=gpt-realtime&" in setup_target(client, bearer)


async def test_rest_audio_never_inherits_text_genai_usage_or_reference_prices(capture):
    exporter, _ = capture
    text, text_deployment = _model()
    deployment = load_catalog().resolve_deployment(TTS_MODEL).deploymentName
    calls = []
    audio = _wav()
    def response(request):
        calls.append(request)
        if request.url.path.endswith("/audio/speech"):
            return httpx.Response(200, content=audio, headers={"Content-Type": "audio/wav"})
        return httpx.Response(200, json=_native("chat", text.id))
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        gateway = _gateway(http)
        assert await gateway.synthesize_speech(
            deployment=deployment, text="Synthetic speech.", voice="alloy", response_format="wav",
        ) == audio
        assert len(calls) == 1
        assert exporter.get_finished_spans() == ()
        for model in (*GA_MODELS, TTS_MODEL):
            estimate = load_pricing().estimate(model, prompt_tokens=100, completion_tokens=20)
            assert not estimate.known and estimate.micro_usd is None
        await gateway.complete(deployment=text_deployment, messages=[])
    spans = exporter.get_finished_spans()
    assert len(calls) == 2 and len(spans) == 1
    assert spans[0].instrumentation_scope.name == INSTRUMENTATION_NAME
    assert spans[0].attributes["gen_ai.request.model"] == text_deployment
    assert spans[0].attributes["ai4ia.gen_ai.usage.coverage"] == "known"
