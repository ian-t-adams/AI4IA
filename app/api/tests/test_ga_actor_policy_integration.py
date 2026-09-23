"""Signed actor reductions intersect GA routing without fencing owner cleanup."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.websockets import WebSocketDisconnect

from ai4ia_api.auth import dependencies as auth_dependencies
from ai4ia_api.policy.context import current_binding
from ai4ia_api.policy.models import PolicyError, parse_policy_config, policy_digest
from ai4ia_api.publishing.refs import AssetVersionRef
from ai4ia_api.realtime_canary import SETUP_INPUT
from ai4ia_api.realtime_protocol import RealtimeProtocol
from tests.test_auth_entra import TENANT
from tests.test_ga_voice_migration import GA_MODELS
from tests.test_policy_actor_restrictions import (
    MONITOR, ORDINARY, REALTIME, ORIGIN, actor_app as actor_app, authorization, fresh_request,
)
from tests.test_policy_binding_availability import assert_unavailable
from tests.test_publication_execution_api import (
    SUBJECTS, publish, published_api as published_api,
)
from tests.test_realtime_canary_integration import SetupConnector
from tests.test_realtime_staged_api import GA_SETTINGS

RETAINED_MODEL = "gpt-realtime-2"
DEFAULT_MODEL = "gpt-realtime"


def setup_exchange(client, bearer, model=None, *, allowed=True, session=None):
    state = client.app.state
    connector = SetupConnector()
    state.realtime_connector = connector
    target = "/api/voice/live?provider=azure_openai"
    if model is not None:
        target += f"&model={model}&region=eastus2"
    if session is not None:
        target += f"&session={session}"
    protocols = ["ai4ia-bearer", bearer]
    if not allowed:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(target, subprotocols=protocols, headers={"origin": ORIGIN}) as ws:
                ws.receive_text()
        assert not connector.calls and not connector.upstream.sent_text
        return
    with client.websocket_connect(target, subprotocols=protocols, headers={"origin": ORIGIN}) as ws:
        protocol = state.settings.realtime_protocol
        assert dict(ws.extra_headers)[b"x-ai4ia-realtime-protocol"] == protocol.value.encode()
        assert json.loads(ws.receive_text())["type"] == "session.created"
        ws.send_text(SETUP_INPUT)
        assert json.loads(ws.receive_text())["type"] == "session.updated"
    assert len(connector.calls) == len(connector.upstream.sent_text) == 1
    assert not connector.upstream.sent_bytes and connector.upstream.closed
    selected = state.catalog.resolve_deployment(model or DEFAULT_MODEL, region="eastus2")
    url = urlsplit(connector.calls[0]["url"])
    ga = state.settings.realtime_protocol == RealtimeProtocol.ga
    base = urlsplit(state.settings.realtime_ga_base_url if ga else state.settings.realtime_effective_base_url)
    assert (url.scheme, url.netloc, url.path) == ("wss", base.netloc, base.path + "/realtime")
    assert parse_qs(url.query)["model" if ga else "deployment"] == [selected.deploymentName]


@pytest.mark.parametrize("cached_actor", [False, True])
@pytest.mark.parametrize("model_id", GA_MODELS)
def test_signed_actor_categories_preserve_runtime_protocol_and_default_routing(
    actor_app, monkeypatch, cached_actor, model_id,
):
    client, model, token, calls, _connector, config = actor_app
    state = client.app.state
    state.catalog = state.policy.catalog = state.catalog.model_copy(deep=True)
    state.agent_service._catalog = state.catalog
    ordinary, actor = token(ORDINARY), token(REALTIME)
    old = deepcopy(config)
    if not cached_actor:
        del old["realtimeCanaryActor"]
    state.settings.group_policy_json = json.dumps(old)
    assert client.get("/api/models", headers=authorization(ordinary)).status_code == 200
    state.settings.group_policy_json = json.dumps(config)
    bind = auth_dependencies.bind_authenticated
    bindings = []

    def observed_binding(service, user):
        bind(service, user)
        bindings.append(current_binding())

    monkeypatch.setattr(auth_dependencies, "bind_authenticated", observed_binding)
    retained = state.catalog.get(RETAINED_MODEL)
    assert retained.runtimeEnabled is True and retained.requiredRealtimeProtocol is None
    for protocol in RealtimeProtocol:
        state.settings.realtime_protocol = protocol
        offered = client.get("/api/models?protocol=ga", headers=authorization(actor))
        assert offered.status_code == 200, offered.text
        assert bindings[-1].restricted_profile == "realtime-setup-canary"
        assert bindings[-1].actor_policy_digest == policy_digest(parse_policy_config(json.dumps(config)))
        rows = offered.json()["models"]
        assert {row["category"] for row in rows} == {"realtime"}
        assert RETAINED_MODEL in {row["id"] for row in rows}
        assert (model_id in {row["id"] for row in rows}) is (protocol == RealtimeProtocol.ga)
        ordinary_rows = client.get("/api/models", headers=authorization(ordinary)).json()["models"]
        assert model.id in {row["id"] for row in ordinary_rows}
        assert RETAINED_MODEL in {row["id"] for row in ordinary_rows}
        assert (model_id in {row["id"] for row in ordinary_rows}) is (protocol == RealtimeProtocol.ga)
        setup_exchange(client, ordinary)
        setup_exchange(client, actor, RETAINED_MODEL, allowed=protocol == RealtimeProtocol.ga)
        setup_exchange(client, ordinary, RETAINED_MODEL)
        setup_exchange(client, actor, model_id, allowed=protocol == RealtimeProtocol.ga)
        setup_exchange(client, ordinary, model_id, allowed=protocol == RealtimeProtocol.ga)
        retained.runtimeEnabled = False
        for bearer in (actor, ordinary):
            assert RETAINED_MODEL not in {
                row["id"] for row in client.get("/api/models", headers=authorization(bearer)).json()["models"]
            }
            setup_exchange(client, bearer, RETAINED_MODEL, allowed=False)
        retained.runtimeEnabled = True
        setup_exchange(client, actor, RETAINED_MODEL, allowed=protocol == RealtimeProtocol.ga)
    actor_tools = client.get("/api/tools", headers=authorization(actor)).json()["tools"]
    ordinary_tools = client.get("/api/tools", headers=authorization(ordinary)).json()["tools"]
    assert actor_tools and not any(row["available"] or row["selectable"] for row in actor_tools)
    assert any(row["name"] == "calculator" and row["available"] for row in ordinary_tools)
    config["realtimeCanaryActor"]["restrictions"]["models"] = [model.category]
    state.settings.group_policy_json = json.dumps(config)
    assert model_id not in {
        row["id"] for row in client.get("/api/models", headers=authorization(actor)).json()["models"]
    }
    setup_exchange(client, actor, model_id, allowed=False)
    setup_exchange(client, ordinary, model_id)
    config["realtimeCanaryActor"]["restrictions"]["models"] = ["realtime"]
    state.settings.group_policy_json = json.dumps(config)
    with monkeypatch.context() as patch:
        patch.setattr(state, "realtime_canary_dispatch_guard", None)
        setup_exchange(client, actor, model_id, allowed=False)
    setup_exchange(client, actor, model_id)
    assert not calls


@pytest.mark.parametrize("subject", [ORDINARY, MONITOR])
@pytest.mark.parametrize("transition", ["unchanged", "restored", "paused"])
def test_unrunnable_catalog_keeps_failed_binding_unavailable_and_owner_cleanup_available(
    actor_app, monkeypatch, subject, transition,
):
    client, model, token, calls, connector, config = actor_app
    state = client.app.state
    bearer = token(subject)
    headers = authorization(bearer)
    body = fresh_request(client, model, bearer)
    path = f"/api/sessions/{body['sessionId']}"
    control = client.post("/api/chat", headers=headers, json=body)
    assert control.status_code == 200 and len(calls) == 1, control.text
    saved = client.get(path, headers=headers).json()
    original_catalog = state.catalog
    state.catalog = state.policy.catalog = original_catalog.model_copy(deep=True)
    for entry in state.catalog.models:
        entry.runtimeEnabled = False
    healthy = client.get("/api/models", headers=headers)
    assert healthy.status_code == 200 and healthy.json()["models"] == []
    valid = json.dumps(config)
    refresh = state.policy.actor_restriction_digest
    refreshes = []

    def transition_on_failed_refresh(user, *, cached=False):
        refreshes.append(cached)
        try:
            return refresh(user, cached=cached)
        except PolicyError:
            if transition == "restored":
                state.settings.group_policy_json = valid
            elif transition == "paused":
                state.settings.group_policy_enabled = False
            raise

    monkeypatch.setattr(state.policy, "actor_restriction_digest", transition_on_failed_refresh)

    def failed_binding_request(method, target, **kwargs):
        state.settings.group_policy_enabled = True
        state.settings.group_policy_json = "{"
        start = len(refreshes)
        response = client.request(method, target, headers=headers, **kwargs)
        assert refreshes[start:start + 2] == [False, True]
        return response

    for target in ("/api/models", "/api/tools"):
        assert_unavailable(failed_binding_request("GET", target))
    # Reach policy admission rather than replaying a consumed fresh-turn claim
    # or stopping at an unavailable model before the execution check.
    disabled_catalog = state.catalog
    state.catalog = state.policy.catalog = original_catalog
    assert_unavailable(failed_binding_request(
        "POST", "/api/chat", json={**body, "requireFreshSession": False},
    ))
    state.catalog = state.policy.catalog = disabled_catalog
    assert failed_binding_request("GET", path).json() == saved
    messages = failed_binding_request("GET", f"{path}/messages")
    assert messages.status_code == 200 and len(messages.json()) == 2
    usage = failed_binding_request("GET", f"/api/usage/sessions/{body['sessionId']}")
    assert usage.status_code == 200 and usage.json()["totalTokens"] == 9
    deleted = failed_binding_request("DELETE", path)
    assert deleted.status_code == 202 and deleted.json()["state"] == "pending"
    for _ in range(2):
        deleted = failed_binding_request("POST", f"{path}/deletion/reconcile")
        assert deleted.status_code in (200, 202), deleted.text
        if deleted.status_code == 200:
            break
    proof = deleted.json()
    assert deleted.status_code == 200 and proof["state"] == "cleanup_verified"
    assert proof["sessionId"] == body["sessionId"]
    assert all(proof[key] is True for key in ("messagesVerified", "documentsVerified", "attachmentsVerified"))
    assert proof["pendingUploads"] == [] and proof["pendingUploadsTruncated"] is False
    assert failed_binding_request("GET", f"{path}/deletion").json() == proof
    assert failed_binding_request("GET", "/api/sessions").json() == []
    assert len(calls) == 1 and not connector.calls
    state.settings.group_policy_enabled = True
    state.settings.group_policy_json = valid
    state.catalog = state.policy.catalog = original_catalog
    restored = client.get("/api/models", headers=headers)
    assert restored.status_code == 200 and restored.json()["models"]
    control = client.post("/api/chat", headers=headers, json=fresh_request(client, model, bearer))
    assert control.status_code == 200 and len(calls) == 2, control.text


@pytest.mark.parametrize("model_id", GA_MODELS)
def test_actor_category_reduction_cannot_erase_published_ga_source_requirements(published_api, model_id):
    client, _model, headers, calls = published_api
    state = client.app.state
    state.settings.realtime_enabled = True
    state.settings.realtime_protocol = RealtimeProtocol.ga
    state.settings.realtime_allowed_origins = ORIGIN
    state.settings.realtime_base_url = "https://realtime-gateway.test/openai"
    for key, value in GA_SETTINGS.items():
        setattr(state.settings, key, value)
    published, source = publish(
        client, model_id, headers, "agent", "actor-policy-ga", tools=False, modes=["voice"],
    )
    reference = AssetVersionRef.model_validate(source)
    _, version = asyncio.run(state.publications._version(reference))
    binding = next(row for row in version.modelBindings if row.modelId == model_id)
    assert binding.runtimeEnabled is True and binding.requiredRealtimeProtocol == "ga"
    assert binding.option.modelVersion == GA_MODELS[model_id]
    original_version = version.model_dump(mode="json")
    auth = headers("Consumer")
    bearer = auth["Authorization"].split(" ", 1)[1]
    created = client.post("/api/sessions", headers=auth, json={
        "model": model_id, "agentName": published["handle"], "libraryDocumentIds": [],
    })
    assert created.status_code == 201, created.text
    session = created.json()["id"]
    setup_exchange(client, bearer, model_id, session=session)
    original_policy = state.settings.group_policy_json
    config = json.loads(original_policy)
    config["realtimeCanaryActor"] = {
        "tenantId": TENANT, "subject": SUBJECTS["Consumer"],
        "restrictions": {"models": ["realtime"], "spend": {"requestsPerMinute": 50}},
    }
    state.settings.group_policy_json = json.dumps(config)
    offered = client.get("/api/models", headers=auth)
    assert offered.status_code == 200, offered.text
    row = next(row for row in offered.json()["models"] if row["id"] == model_id)
    assert row.get("runtimeEnabled", True) is True and row["requiredRealtimeProtocol"] == "ga"
    assert {row["category"] for row in offered.json()["models"]} == {"realtime"}
    setup_exchange(client, bearer, model_id, session=session, allowed=False)
    _, version = asyncio.run(state.publications._version(reference))
    assert version.model_dump(mode="json") == original_version
    state.settings.group_policy_json = original_policy
    setup_exchange(client, bearer, model_id, session=session)
    assert not calls
