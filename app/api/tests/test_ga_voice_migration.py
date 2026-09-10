"""Source migration and real API/relay controls; all transports are synthetic."""
from __future__ import annotations

import asyncio
import io
import json
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import httpx
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from ai4ia_api.catalog import ModelCatalog, ModelEntry, load_catalog
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.realtime_protocol import RealtimeProtocol
from ai4ia_api.routers.realtime import (
    DEV_SUBPROTOCOL,
    RealtimeResolutionError,
    UpstreamMessage,
    resolve_realtime_deployment,
)
from ai4ia_api.voice_provider_catalog import load_voice_provider_catalog
from tests.test_realtime_api import ScriptedRealtimeConnector, _client, _origin, _speech_client
from tests.test_realtime_staged_api import GA_SETTINGS, _echo
from tests.conftest import make_settings

HEADERS = {"X-Dev-User": "alice"}
GA_MODEL = "gpt-realtime-1.5"
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def client():
    c = _client(realtime_enabled=True, **GA_SETTINGS)
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def test_catalog_delta_keeps_footprint_default_and_existing_tts_capacity():
    source = json.loads((ROOT / "infra/models.json").read_text(encoding="utf-8"))
    models = {m["name"]: m for m in source["catalog"]}
    assert models["gpt-realtime-2"] == {
        "name": "gpt-realtime-2", "format": "OpenAI", "category": "realtime",
        "runtimeEnabled": False,
        "deployments": [{
            "region": "eastus2", "sku": "GlobalStandard", "capacity": 10,
            "version": "2026-05-06", "maxCapacity": 10, "maxCapacityPool": "global",
        }],
    }
    assert [m["name"] for m in models.values() if m.get("runtimeEnabled") is False] == ["gpt-realtime-2"]
    assert models[GA_MODEL]["requiredRealtimeProtocol"] == "ga"
    assert models[GA_MODEL]["deployments"] == [{
        "region": "eastus2", "sku": "GlobalStandard", "capacity": 10, "version": "2026-02-23",
    }]
    assert models["gpt-4o-mini-tts"]["deployments"] == [{
        "region": "eastus2", "sku": "GlobalStandard", "capacity": 10,
        "version": "2025-12-15", "maxCapacity": 600, "maxCapacityPool": "global",
    }]
    catalog = load_catalog()
    assert catalog.get("gpt-realtime-2") is None
    assert catalog.get(GA_MODEL).requiredRealtimeProtocol == "ga"
    for protocol in RealtimeProtocol:
        selected, _ = resolve_realtime_deployment(catalog, None, None, protocol=protocol)
        assert selected == "gpt-realtime"
    tts = catalog.resolve_deployment("gpt-4o-mini-tts")
    assert tts.deploymentName == (
        f"gpt-4o-mini-tts-{source['naming']['subscriptionToken']}-eastus2-glbl"
    )


def test_ga_requirement_is_not_a_client_protocol_selector(client):
    settings = client.app.state.settings
    connector = client.app.state.realtime_connector
    query = f"?model={GA_MODEL}&protocol=ga"
    assert settings.realtime_protocol == RealtimeProtocol.preview
    published = client.get("/api/models?protocol=ga", headers=HEADERS).json()["models"]
    assert GA_MODEL not in {m["id"] for m in published}
    assert "gpt-realtime" in {m["id"] for m in published}
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect(
            f"/api/voice/live{query}", headers=_origin(),
            subprotocols=[DEV_SUBPROTOCOL, "alice"],
        ):
            pass
    assert denied.value.code == 1008
    assert "requires the GA protocol" in denied.value.reason
    assert connector.connects == []

    settings.realtime_protocol = RealtimeProtocol.ga
    published = client.get("/api/models", headers=HEADERS).json()["models"]
    assert next(m for m in published if m["id"] == GA_MODEL)["requiredRealtimeProtocol"] == "ga"
    _echo(client, query=query, user="alice")
    assert len(connector.connects) == 1
    opened = connector.connects[0]
    target = client.app.state.catalog.resolve_deployment(GA_MODEL).deploymentName
    url = urlsplit(opened["url"])
    assert url.path == "/openai/v1/realtime"
    assert parse_qs(url.query) == {"model": [target]}
    assert opened["headers"]["Ocp-Apim-Subscription-Key"] == GA_SETTINGS["realtime_ga_gateway_api_key"]

    settings.realtime_ga_enabled = False
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/api/voice/live{query}", headers=_origin(),
            subprotocols=[DEV_SUBPROTOCOL, "alice"],
        ):
            pass
    assert len(connector.connects) == 1
    settings.realtime_ga_enabled = True
    _echo(client, query=query, user="alice")
    assert len(connector.connects) == 2


@pytest.mark.parametrize("protocol", list(RealtimeProtocol))
def test_removed_saved_model_is_rejected_without_changing_a_conversation(client, protocol):
    client.app.state.settings.realtime_protocol = protocol
    created = client.post("/api/sessions", headers=HEADERS, json={"title": "Existing voice"})
    assert created.status_code == 201
    session_id = created.json()["id"]
    before = client.get(f"/api/sessions/{session_id}", headers=HEADERS).json()
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect(
            f"/api/voice/live?session={session_id}&model=gpt-realtime-2",
            headers=_origin(), subprotocols=[DEV_SUBPROTOCOL, "alice"],
        ):
            pass
    assert "Choose an available model" in denied.value.reason
    assert client.app.state.realtime_connector.connects == []
    assert client.get(f"/api/sessions/{session_id}", headers=HEADERS).json() == before
    _echo(client, query=f"?session={session_id}&model=gpt-realtime", user="alice")
    assert len(client.app.state.realtime_connector.connects) == 1


def test_default_resolver_cannot_fall_back_to_a_ga_only_catalog():
    entry = load_catalog().get(GA_MODEL)
    catalog = ModelCatalog(models=[entry])
    with pytest.raises(RealtimeResolutionError, match="No realtime models"):
        resolve_realtime_deployment(catalog, None, None)
    assert resolve_realtime_deployment(
        catalog, None, None, protocol=RealtimeProtocol.ga,
    )[0] == GA_MODEL


@pytest.mark.parametrize("patch", [
    {"category": "chat"}, {"requiredRealtimeProtocol": "unsupported"},
    {"runtimeEnabled": "false"}, {"runtimeEnabled": 0}, {"runtimeEnabled": None},
])
def test_runtime_rejects_malformed_protocol_requirements(patch):
    entry = load_catalog().get(GA_MODEL)
    with pytest.raises(ValidationError):
        ModelEntry.model_validate({**entry.model_dump(), **patch})


def test_disabled_inventory_has_no_runtime_lookup_routing_or_tools():
    source = load_catalog().get("gpt-5.2")
    assert source is not None
    entry = source.model_copy(deep=True)
    catalog = ModelCatalog(models=[entry])
    assert catalog.get(entry.id) is entry
    assert catalog.resolve_deployment(entry.id) is not None
    assert entry in catalog.conversational_models()
    assert entry.supportsTools
    entry.runtimeEnabled = False
    assert catalog.get(entry.id) is None
    assert catalog.resolve_deployment(entry.id) is None
    assert catalog.eligible_options(entry) == []
    assert not catalog.available(entry)
    assert catalog.conversational_models() == []
    assert not entry.conversational and not entry.supportsTools
    assert not entry.supports_realtime_protocol("ga")
    assert entry in catalog.models  # Retained desired metadata is not deleted.


@pytest.mark.parametrize("protocol", list(RealtimeProtocol))
def test_enabled_voice_requires_a_runtime_compatible_model_at_startup(tmp_path, protocol):
    entry = load_catalog().get(GA_MODEL)
    source = ModelCatalog(models=[entry]).model_dump()
    path = tmp_path / "models.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    settings = make_settings(
        model_catalog_path=str(path), realtime_enabled=True, realtime_protocol=protocol, **GA_SETTINGS,
    )
    if protocol == RealtimeProtocol.preview:
        with pytest.raises(RuntimeError, match="runtime-enabled realtime model"):
            settings._validate_data_residency()
    else:
        settings._validate_data_residency()
    # The identical catalog remains valid when the dependent feature is off.
    settings.realtime_enabled = False
    settings._validate_data_residency()


@pytest.mark.parametrize("field", [
    "memory_embedding_model", "memory_extraction_model", "cu_completion_model", "cu_embedding_model",
])
def test_runtime_required_models_cannot_be_disabled_even_under_global_residency(tmp_path, field):
    settings = make_settings(
        memory_store="cosmos", cosmos_endpoint="https://example.documents.azure.com/",
        document_understanding_enabled=True,
    )
    source = load_catalog().model_dump()
    target = next(m for m in source["models"] if m["id"] == getattr(settings, field))
    path = tmp_path / "catalog.json"
    target["runtimeEnabled"] = False
    path.write_text(json.dumps(source), encoding="utf-8")
    settings.model_catalog_path = str(path)
    with pytest.raises(RuntimeError, match="runtime-disabled catalog models"):
        settings._validate_data_residency()
    target["runtimeEnabled"] = True
    path.write_text(json.dumps(source), encoding="utf-8")
    load_catalog.cache_clear()
    settings._validate_data_residency()


@pytest.mark.parametrize("cap", ["tokensPerDay", "costPerDayMicroUsd"])
def test_ga_tts_uses_the_existing_proxy_speech_path_and_admission(cap):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\1\0" * 2400)
    audio = output.getvalue()
    sent: list[httpx.Request] = []
    def transport(request):
        sent.append(request)
        return httpx.Response(200, content=audio, headers={"Content-Type": "audio/wav"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    c = _client(
        model_gateway_url="https://proxy.example.test/openai",
        model_gateway_api_key_header="S7P-KEY",
        hard_quota_enabled=True, entitlements_enabled=False,
    )
    try:
        settings = c.app.state.settings
        c.app.state.gateway = ModelGatewayClient(settings, http)
        uid = c.get("/api/entitlement", headers=HEADERS).json()["userId"]
        c.app.state.hard_quota.reservations.store.seed(uid)
        body = {"input": "Synthetic speech check.", "model": "gpt-4o-mini-tts",
                "region": "eastus2", "format": "wav", "voice": "alloy"}
        assert c.put(
            f"/api/admin/entitlements/{uid}", headers=HEADERS, json={cap: 1000},
        ).status_code == 200
        denied = c.post("/api/voice/speech", headers=HEADERS, json=body)
        assert denied.status_code == 503 and "does not support" in denied.text
        assert sent == []
        assert c.put(
            f"/api/admin/entitlements/{uid}", headers=HEADERS, json={"requestsPerMinute": 1},
        ).status_code == 200
        response = c.post("/api/voice/speech", headers=HEADERS, json=body)
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.headers["x-model"] == "gpt-4o-mini-tts"
        assert response.content == audio
        assert len(sent) == 1
        deployment = c.app.state.catalog.resolve_deployment("gpt-4o-mini-tts").deploymentName
        assert str(sent[0].url) == (
            f"https://proxy.example.test/openai/deployments/{deployment}/audio/speech"
            f"?api-version={settings.gateway_audio_api_version}"
        )
        assert sent[0].headers["S7P-KEY"] == "proxy-ingress-key"
        assert json.loads(sent[0].content) == {
            "input": body["input"], "model": deployment, "voice": "alloy", "response_format": "wav",
        }
    finally:
        c.__exit__(None, None, None)
        asyncio.run(http.aclose())


def test_ga_replacement_does_not_expand_speech_managed_models():
    provider = load_voice_provider_catalog().get("speech_voice_live")
    assert provider.defaultManagedModelId == "gpt-realtime"
    assert provider.get_managed_model(GA_MODEL) is None
    c = _speech_client(realtime_protocol="ga", **GA_SETTINGS)
    try:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(
                f"/api/voice/live?provider=speech_voice_live&model={GA_MODEL}",
                headers=_origin(), subprotocols=[DEV_SUBPROTOCOL, "alice"],
            ):
                pass
        assert c.app.state.realtime_connector.connects == []
        with c.websocket_connect(
            "/api/voice/live?provider=speech_voice_live&model=gpt-realtime",
            headers=_origin(), subprotocols=[DEV_SUBPROTOCOL, "alice"],
        ) as ws:
            frame = {"type": "input_audio_buffer.append", "audio": "AAA="}
            ws.send_text(json.dumps(frame))
            assert json.loads(ws.receive_text().removeprefix("echo:")) == frame
        assert len(c.app.state.realtime_connector.connects) == 1
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("cap", ["tokensPerDay", "costPerDayMicroUsd"])
def test_ga_reference_pricing_cannot_authorize_capped_realtime(cap):
    c = _client(
        realtime_enabled=True, realtime_protocol="ga", hard_quota_enabled=True,
        entitlements_enabled=False, **GA_SETTINGS,
    )
    try:
        uid = c.get("/api/entitlement", headers=HEADERS).json()["userId"]
        c.app.state.hard_quota.reservations.store.seed(uid)
        connector = ScriptedRealtimeConnector([UpstreamMessage("close", close_code=1000)])
        c.app.state.realtime_connector = connector
        def connect():
            with c.websocket_connect(
                f"/api/voice/live?model={GA_MODEL}", headers=_origin(),
                subprotocols=[DEV_SUBPROTOCOL, "alice"],
            ) as ws:
                for _ in range(10):
                    if ws.receive()["type"] == "websocket.close":
                        return
                raise AssertionError("Fixture relay did not terminate")
        assert c.put(
            f"/api/admin/entitlements/{uid}", headers=HEADERS, json={cap: 1000},
        ).status_code == 200
        connect()
        assert connector.connects == []
        assert c.put(
            f"/api/admin/entitlements/{uid}", headers=HEADERS, json={"requestsPerMinute": 1},
        ).status_code == 200
        connect()
        assert len(connector.connects) == 1
    finally:
        c.__exit__(None, None, None)
