"""Real policy/factory/transport integration for separately bound model-only actors."""
from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app
from ai4ia_api.request_constraints import CANARY_SENTINEL
from tests.conftest import make_settings
from tests.test_auth_entra import BARE_GUID, ISSUER, KID, TENANT, _new_keypair, _provider

MONITOR = "00000000-0000-0000-0000-000000000001"
EVALUATOR = "00000000-0000-0000-0000-000000000002"


@pytest.fixture
def profiles():
    from ai4ia_api.catalog import load_catalog

    model = next(
        item for item in load_catalog().models
        if item.conversational and item.api == "chat" and not item.reasoningEffortOptions
    )
    config = {
        "canaryActor": {"tenantId": TENANT, "subject": MONITOR},
        "evaluationActor": {"tenantId": TENANT, "subject": EVALUATOR},
        "domains": {
            "models": {"default": {"allow": [model.category]}},
            "tools": {"default": {"allow": []}},
            "documents": {"default": {"allow": []}},
        },
        "spend": {"default": {"requestsPerMinute": 50}},
    }
    settings = make_settings(
        auth_provider="entra", entra_tenant_id=TENANT, entra_audience=BARE_GUID,
        group_policy_enabled=True, group_policy_json=json.dumps(config),
        session_deletion_enabled=True,
    )
    private, jwks = _new_keypair(KID)
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ready"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
        })

    def headers(subject):
        now = int(time.time())
        token = jwt.encode({
            "aud": BARE_GUID, "iss": ISSUER, "tid": TENANT, "oid": subject,
            "iat": now, "exp": now + 3600, "roles": [],
        }, private, algorithm="RS256", headers={"kid": KID})
        return {"Authorization": f"Bearer {token}"}

    app = create_app(settings)
    with TestClient(app) as client:
        app.state.auth_provider = _provider(jwks, audience=BARE_GUID)
        app.state.gateway = ModelGatewayClient(
            settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        yield client, model.id, headers, calls


@pytest.mark.parametrize("profile,subject,prompt,maximum", [
    ("monitor-canary", MONITOR, CANARY_SENTINEL, 64),
    ("authored-synthetic-evaluation", EVALUATOR, "What is seven plus five?", 256),
])
def test_real_profile_probe_and_one_shot_dispatch(profiles, profile, subject, prompt, maximum):
    client, model, headers, calls = profiles
    auth = headers(subject)
    capability = client.get(
        "/api/execution-capabilities", params={"profile": profile, "model": model}, headers=auth,
    )
    assert capability.status_code == 200, capability.text
    assert capability.json()["ready"] is True
    assert capability.json()["ownerBound"] is True
    assert capability.json()["constraints"]["maxOutputTokens"] == maximum
    wrong = "authored-synthetic-evaluation" if profile == "monitor-canary" else "monitor-canary"
    assert client.get(
        "/api/execution-capabilities", params={"profile": wrong, "model": model}, headers=auth,
    ).json()["ready"] is False
    session = client.post("/api/sessions", json={
        "model": model, "libraryDocumentIds": [],
    }, headers=auth).json()
    body = {
        "sessionId": session["id"], "content": prompt, "model": model, "stream": False,
        "allowTools": False, "allowAutomaticMemory": False, "requireFreshSession": True,
        "params": {"max_tokens": maximum},
    }
    response = client.post("/api/chat", json=body, headers=auth)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert calls[0]["messages"] == [{"role": "user", "content": prompt}]
    assert "tools" not in calls[0]
    assert client.post("/api/chat", json=body, headers=auth).status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["tools", "memory", "fresh", "sentinel", "output", "missing_guard"])
def test_monitor_cannot_bypass_profile_at_actual_dispatch(profiles, change):
    client, model, headers, calls = profiles
    auth = headers(MONITOR)
    session = client.post("/api/sessions", json={
        "model": model, "libraryDocumentIds": [],
    }, headers=auth).json()
    body = {
        "sessionId": session["id"], "content": CANARY_SENTINEL, "model": model,
        "stream": False, "allowTools": False, "allowAutomaticMemory": False,
        "requireFreshSession": True, "params": {"max_tokens": 64},
    }
    if change in {"tools", "memory", "fresh"}:
        key = {"tools": "allowTools", "memory": "allowAutomaticMemory", "fresh": "requireFreshSession"}[change]
        body[key] = change != "fresh"
    elif change == "sentinel":
        body["content"] = "A different authored prompt."
    elif change == "output":
        body["params"]["max_tokens"] = 65
    else:
        client.app.state.canary_dispatch_guard = None
    response = client.post("/api/chat", json=body, headers=auth)
    assert response.status_code >= 400, response.text
    assert calls == []
