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


def provider_reply(request: httpx.Request) -> httpx.Response:
    """Answer in the native shape of the provider operation actually called.

    The least-estimated-cost canary may select either a Chat Completions or a
    Responses model from the catalog, so a fixed Chat Completions body would
    fail the Responses translator instead of exercising it.
    """
    if request.url.path.endswith("/responses"):
        return httpx.Response(200, json={
            "status": "completed",
            "output": [{
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "ready"}],
            }],
            "usage": {"input_tokens": 8, "output_tokens": 1, "total_tokens": 9},
        })
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": "ready"}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
    })


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
        return provider_reply(request)

    def headers(subject, **claims):
        now = int(time.time())
        token = jwt.encode({
            "aud": BARE_GUID, "iss": ISSUER, "tid": TENANT, "oid": subject,
            "iat": now, "exp": now + 3600, "roles": [], **claims,
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


@pytest.mark.parametrize("subject,prompt,maximum,guard", [
    (MONITOR, CANARY_SENTINEL, 64, "canary_dispatch_guard"),
    (EVALUATOR, "What is seven plus five?", 256, "evaluation_dispatch_guard"),
])
@pytest.mark.parametrize("change", ["tools", "memory", "fresh", "prompt", "output", "missing_guard"])
def test_retained_actor_cannot_bypass_profile_at_actual_dispatch(
    profiles, subject, prompt, maximum, guard, change,
):
    client, model, headers, calls = profiles
    auth = headers(subject)

    def request():
        session = client.post("/api/sessions", json={
            "model": model, "libraryDocumentIds": [],
        }, headers=auth)
        assert session.status_code == 201, session.text
        return {
            "sessionId": session.json()["id"], "content": prompt, "model": model,
            "stream": False, "allowTools": False, "allowAutomaticMemory": False,
            "requireFreshSession": True, "params": {"max_tokens": maximum},
        }

    control = client.post("/api/chat", json=request(), headers=auth)
    assert control.status_code == 200, control.text
    assert len(calls) == 1
    body = request()
    if change in {"tools", "memory", "fresh"}:
        key = {"tools": "allowTools", "memory": "allowAutomaticMemory", "fresh": "requireFreshSession"}[change]
        body[key] = change != "fresh"
    elif change == "prompt":
        body["content"] = "A different authored prompt." if subject == MONITOR else "x" * 4097
    elif change == "output":
        body["params"]["max_tokens"] = maximum + 1
    else:
        setattr(client.app.state, guard, None)
    response = client.post("/api/chat", json=body, headers=auth)
    assert response.status_code >= 400, response.text
    assert len(calls) == 1


@pytest.mark.parametrize("subject,profile,prompt,maximum", [
    (MONITOR, "monitor-canary", CANARY_SENTINEL, 64),
    (EVALUATOR, "authored-synthetic-evaluation", "What is seven plus five?", 256),
])
@pytest.mark.parametrize("transition", ["paused", "malformed"])
def test_retiring_clients_cannot_relax_configured_actor_denials_or_owner_cleanup(
    profiles, subject, profile, prompt, maximum, transition,
):
    client, model, headers, calls = profiles
    auth = headers(subject)
    capability = client.get(
        "/api/execution-capabilities", params={"profile": profile, "model": model}, headers=auth,
    )
    assert capability.status_code == 200 and capability.json()["ready"]
    session = client.post("/api/sessions", json={
        "model": model, "libraryDocumentIds": [],
    }, headers=auth)
    assert session.status_code == 201, session.text
    session_id = session.json()["id"]
    body = {
        "sessionId": session_id, "content": prompt, "model": model, "stream": False,
        "allowTools": False, "allowAutomaticMemory": False, "requireFreshSession": True,
        "params": {"max_tokens": maximum},
    }
    control = client.post("/api/chat", json=body, headers=auth)
    assert control.status_code == 200, control.text
    assert len(calls) == 1
    session = client.post("/api/sessions", json={
        "model": model, "libraryDocumentIds": [],
    }, headers=auth)
    assert session.status_code == 201, session.text
    session_id = session.json()["id"]
    body["sessionId"] = session_id
    settings = client.app.state.settings
    if transition == "paused":
        settings.group_policy_enabled = False
    else:
        settings.group_policy_json = "{invalid"
    response = client.post("/api/chat", json=body, headers=auth)
    assert response.status_code >= 400, response.text
    assert len(calls) == 1
    foreign = headers(EVALUATOR if subject == MONITOR else MONITOR)
    path = f"/api/sessions/{session_id}"
    assert client.get(path, headers=foreign).status_code == 404
    assert client.delete(path, headers=foreign).status_code == 404
    assert client.get(path, headers=auth).status_code == 200
    assert client.delete(path, headers=auth).status_code == 202
    for _ in range(2):
        cleanup = client.post(f"{path}/deletion/reconcile", headers=auth)
        assert cleanup.status_code in (200, 202), cleanup.text
        if cleanup.status_code == 200:
            break
    assert cleanup.status_code == 200 and cleanup.json()["state"] == "cleanup_verified"
    assert client.get(f"{path}/deletion", headers=auth).json() == cleanup.json()
    assert len(calls) == 1


@pytest.mark.parametrize("subject,profile,prompt,maximum", [
    (MONITOR, "monitor-canary", CANARY_SENTINEL, 64),
    (EVALUATOR, "authored-synthetic-evaluation", "What is seven plus five?", 256),
])
@pytest.mark.parametrize("invalid", ["expired", "admin"])
def test_retained_actor_requires_unexpired_nonprivileged_signed_identity(
    profiles, subject, profile, prompt, maximum, invalid,
):
    client, model, headers, calls = profiles
    auth = headers(subject)
    session = client.post("/api/sessions", json={
        "model": model, "libraryDocumentIds": [],
    }, headers=auth)
    assert session.status_code == 201, session.text
    body = {
        "sessionId": session.json()["id"], "content": prompt, "model": model, "stream": False,
        "allowTools": False, "allowAutomaticMemory": False, "requireFreshSession": True,
        "params": {"max_tokens": maximum},
    }
    invalid_auth = headers(subject, **(
        {"exp": int(time.time()) - 60} if invalid == "expired" else {"roles": ["admin"]}
    ))
    capability = client.get(
        "/api/execution-capabilities", params={"profile": profile, "model": model},
        headers=invalid_auth,
    )
    assert capability.status_code >= 400 or capability.json()["ready"] is False
    response = client.post("/api/chat", json=body, headers=invalid_auth)
    assert response.status_code >= 400, response.text
    assert calls == []
    if invalid == "admin":
        assert client.post("/api/chat", json=body, headers=auth).status_code == 409
    session = client.post("/api/sessions", json={
        "model": model, "libraryDocumentIds": [],
    }, headers=auth)
    assert session.status_code == 201, session.text
    body["sessionId"] = session.json()["id"]
    control = client.post("/api/chat", json=body, headers=auth)
    assert control.status_code == 200, control.text
    assert len(calls) == 1
