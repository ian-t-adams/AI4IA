"""Unavailable execution policy must not make authenticated owner cleanup unavailable."""
from __future__ import annotations

from copy import deepcopy
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from ai4ia_api.auth.base import AuthCredentials
from ai4ia_api.policy.context import (
    bind_authenticated, clear_policy_context, current_binding, model_allowed,
    require_policy, tool_allowed,
)
from ai4ia_api.policy.dispatch import authorize_dispatch
from ai4ia_api.policy.models import PolicyDecision, PolicyError, PolicyRequest
from ai4ia_api.request_constraints import CANARY_SENTINEL
from ai4ia_api.sessions.models import Message
from ai4ia_api.usage.models import TokenUsage
from tests.test_policy_actor_restrictions import (
    EVALUATOR, MONITOR, ORDINARY, REALTIME, actor_app as actor_app,
    ORIGIN, authorization, exercise_setup, fresh_request, ready, setup_target,
)


def configure(client, model, config, kind):
    selected = deepcopy(config)
    subject = MONITOR
    if kind == "ordinary":
        subject = ORDINARY
        selected = {"domains": {"models": {"default": {"allow": [model.category]}}}}
    elif kind == "legacy":
        for marker in ("canaryActor", "evaluationActor", "realtimeCanaryActor"):
            del selected[marker]["restrictions"]
        selected.update({
            "domains": {
                "models": {"default": {"allow": [model.category]}},
                "tools": {"default": {"allow": []}},
                "documents": {"default": {"allow": []}},
            },
            "spend": {"default": {"requestsPerMinute": 50}},
        })
    client.app.state.settings.group_policy_json = json.dumps(selected)
    return subject, selected


def unavailable_config(valid, kind):
    if kind == "malformed":
        return "{"
    changed = deepcopy(valid)
    if kind == "unknown_model":
        changed["domains"] = {"models": {"default": {"allow": ["not-a-catalog-category"]}}}
    else:
        changed["unknownPolicyField"] = True
    return json.dumps(changed)


def assert_unavailable(response):
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "policy_unavailable"


def cleanup(client, headers, session_id):
    path = f"/api/sessions/{session_id}"
    result = client.delete(path, headers=headers)
    assert result.status_code == 202, result.text
    assert result.json()["state"] == "pending"
    for _ in range(2):
        result = client.post(f"{path}/deletion/reconcile", headers=headers)
        assert result.status_code in (200, 202), result.text
        if result.status_code == 200:
            break
    proof = result.json()
    assert result.status_code == 200 and proof["state"] == "cleanup_verified", proof
    assert proof["sessionId"] == session_id
    assert proof["messagesVerified"] and proof["documentsVerified"] and proof["attachmentsVerified"]
    assert proof["pendingUploads"] == [] and not proof["pendingUploadsTruncated"]
    assert client.get(f"{path}/deletion", headers=headers).json() == proof
    assert client.get("/api/sessions", headers=headers).json() == []
    return proof


@pytest.mark.parametrize("kind", ["ordinary", "legacy", "explicit"])
@pytest.mark.parametrize("invalid", ["malformed", "unknown_model", "unknown_field"])
async def test_current_policy_failure_preserves_owned_reads_accounting_and_verified_v1_cleanup(
    actor_app, kind, invalid,
):
    client, model, token, calls, connector, config = actor_app
    subject, valid = configure(client, model, config, kind)
    bearer = token(subject)
    headers = authorization(bearer)
    catalog = client.get("/api/models", headers=headers)
    assert catalog.status_code == 200 and catalog.json()["models"]
    body = fresh_request(client, model, bearer)
    session = client.get(f"/api/sessions/{body['sessionId']}", headers=headers).json()
    repo = client.app.state.session_repo
    message = Message(
        userId=session["userId"], sessionId=session["id"],
        role="user", content="Persisted synthetic owner content.",
    )
    await repo.add_message(session["userId"], message)
    client.app.state.settings.group_policy_json = unavailable_config(valid, invalid)

    listed = client.get("/api/sessions", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [session["id"]]
    assert client.get(f"/api/sessions/{session['id']}", headers=headers).json() == session
    messages = client.get(f"/api/sessions/{session['id']}/messages", headers=headers)
    assert messages.status_code == 200, messages.text
    assert [(item["id"], item["content"]) for item in messages.json()] == [(message.id, message.content)]

    # Accepted-work accounting requires no fresh execution policy.
    await client.app.state.usage.record_completion(
        user_id=session["userId"], session_id=session["id"], model_id=model.id,
        deployment=model.options[0], usage=TokenUsage.parse({
            "prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9,
        }),
    )
    usage = client.get(f"/api/usage/sessions/{session['id']}", headers=headers)
    assert usage.status_code == 200, usage.text
    assert usage.json()["totalTokens"] == 9
    assert client.get("/api/usage", headers=headers).status_code == 200

    for path in ("/api/models", "/api/tools"):
        assert_unavailable(client.get(path, headers=headers))
    assert_unavailable(client.post(
        "/api/chat", json={**body, "requireFreshSession": False}, headers=headers,
    ))
    assert_unavailable(client.get(f"/api/sessions/{session['id']}/documents", headers=headers))
    assert not calls and not connector.calls

    other = authorization(token(EVALUATOR if subject != EVALUATOR else ORDINARY))
    assert client.get(f"/api/sessions/{session['id']}", headers=other).status_code == 404
    assert client.get(f"/api/sessions/{session['id']}/messages", headers=other).status_code == 404
    assert client.delete(f"/api/sessions/{session['id']}", headers=other).status_code == 404
    proof = cleanup(client, headers, session["id"])
    assert client.get("/api/sessions/deletions", headers=headers).json()["items"] == [proof]
    assert not calls and not connector.calls

    client.app.state.settings.group_policy_json = json.dumps(valid)
    assert client.get("/api/models", headers=headers).json() == catalog.json()
    control = fresh_request(client, model, bearer)
    response = client.post("/api/chat", json=control, headers=headers)
    assert response.status_code == 200, response.text
    assert len(calls) == 1 and not connector.calls
    cleanup(client, headers, control["sessionId"])


def test_unavailable_model_inventory_is_not_reported_as_a_healthy_empty_catalog(actor_app):
    client, model, token, calls, connector, config = actor_app
    subject, _valid = configure(client, model, config, "ordinary")
    headers = authorization(token(subject))
    assert client.get("/api/models", headers=headers).json()["models"]
    models = client.app.state.catalog.models
    client.app.state.settings.group_policy_json = "{"
    client.app.state.catalog.models = []
    try:
        assert_unavailable(client.get("/api/models", headers=headers))
        assert client.get("/api/sessions", headers=headers).status_code == 200
    finally:
        client.app.state.catalog.models = models
    assert not calls and not connector.calls


def test_unavailable_tool_policy_refuses_before_tool_inventory_is_read(actor_app, monkeypatch):
    client, model, token, calls, connector, config = actor_app
    subject, valid = configure(client, model, config, "ordinary")
    headers = authorization(token(subject))
    registry = client.app.state.tool_registry
    original = registry.list
    reads = []

    def observed_list():
        reads.append(True)
        return original()

    monkeypatch.setattr(registry, "list", observed_list)
    control = client.get("/api/tools", headers=headers)
    assert control.status_code == 200 and control.json()["tools"] and reads
    reads.clear()
    client.app.state.settings.group_policy_json = "{"
    assert_unavailable(client.get("/api/tools", headers=headers))
    assert not reads and not calls and not connector.calls
    client.app.state.settings.group_policy_json = json.dumps(valid)
    assert client.get("/api/tools", headers=headers).json() == control.json()
    assert reads


def test_policy_pause_during_failed_binding_cannot_skip_document_authorization(actor_app, monkeypatch):
    client, model, token, calls, connector, config = actor_app
    subject, valid = configure(client, model, config, "ordinary")
    headers = authorization(token(subject))
    body = fresh_request(client, model, token(subject))
    path = f"/api/sessions/{body['sessionId']}/documents"
    assert client.get(path, headers=headers).status_code == 200
    policy = client.app.state.policy
    original = policy.actor_restriction_digest

    def pause_on_unavailable(user, *, cached=False):
        try:
            return original(user, cached=cached)
        except PolicyError:
            client.app.state.settings.group_policy_enabled = False
            raise

    monkeypatch.setattr(policy, "actor_restriction_digest", pause_on_unavailable)
    client.app.state.settings.group_policy_json = "{"
    assert_unavailable(client.get(path, headers=headers))
    assert not client.app.state.settings.group_policy_enabled
    assert not calls and not connector.calls
    client.app.state.settings.group_policy_enabled = True
    client.app.state.settings.group_policy_json = json.dumps(valid)
    assert client.get(path, headers=headers).status_code == 200


@pytest.mark.parametrize("kind", ["ordinary", "legacy", "explicit"])
@pytest.mark.parametrize("transition", ["unchanged", "restored", "removed", "paused"])
async def test_unavailable_binding_is_not_a_dispatch_or_snapshot_grant_after_a_transition(
    actor_app, kind, transition,
):
    client, model, token, calls, connector, config = actor_app
    subject, valid = configure(client, model, config, kind)
    policy = client.app.state.policy
    bearer = token(subject)
    assert client.get("/api/models", headers=authorization(bearer)).status_code == 200
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=bearer))
    client.app.state.settings.group_policy_json = "{"
    bind_authenticated(policy, user)
    try:
        binding = current_binding()
        assert binding is not None and binding.owner_id == user.internal_user_id
        assert binding.user == user and binding.user is not user
        assert binding.restricted_profile == (None if kind == "ordinary" else "monitor-canary")
        assert binding.canary_required is (kind != "ordinary")
        if transition == "restored":
            client.app.state.settings.group_policy_json = json.dumps(valid)
        elif transition == "removed":
            client.app.state.settings.group_policy_json = "{}"
        elif transition == "paused":
            client.app.state.settings.group_policy_enabled = False
        for observe in (
            lambda: model_allowed(model.category, model.options[0]),
            lambda: tool_allowed("calculator"),
        ):
            with pytest.raises(PolicyError, match="policy_unavailable"):
                observe()
        with pytest.raises(PolicyError, match="policy_unavailable"):
            await binding.resolve()
        with pytest.raises(PolicyError, match="policy_unavailable"):
            await require_policy(PolicyRequest("document.read"))
        with pytest.raises(PolicyError):
            await authorize_dispatch(
                "chat", deployment=model.options[0].deploymentName, service=policy,
                expected_owner=user.internal_user_id,
            )
    finally:
        clear_policy_context()
    client.app.state.settings.group_policy_enabled = True
    client.app.state.settings.group_policy_json = json.dumps(valid)
    bind_authenticated(policy, user)
    try:
        assert model_allowed(model.category, model.options[0])
        await require_policy(PolicyRequest("model.invoke", model_id=model.id, deployment=model.options[0]))
    finally:
        clear_policy_context()
    assert not calls and not connector.calls


@pytest.mark.parametrize("profile,subject", [
    ("monitor-canary", MONITOR), ("authored-synthetic-evaluation", EVALUATOR),
    ("realtime-setup-canary", REALTIME),
])
async def test_new_actor_configuration_is_refreshed_at_binding_not_inferred_from_old_owner_status(
    actor_app, profile, subject,
):
    client, _model, token, calls, connector, config = actor_app
    policy = client.app.state.policy
    client.app.state.settings.group_policy_json = "{}"
    bearer = token(subject)
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=bearer))
    bind_authenticated(policy, user)
    assert current_binding().restricted_profile is None
    client.app.state.settings.group_policy_json = json.dumps(config)
    try:
        bind_authenticated(policy, user)
        binding = current_binding()
        assert binding.restricted_profile == profile and binding.actor_policy_digest is not None
        # A valid removal after binding cannot revive ordinary authority.
        client.app.state.settings.group_policy_json = "{}"
        with pytest.raises(PolicyError, match="canary_policy_unconfigured"):
            await binding.resolve()
    finally:
        clear_policy_context()
    assert not calls and not connector.calls


async def test_binding_does_not_hide_nonconfiguration_policy_errors(actor_app, monkeypatch):
    client, _model, token, _calls, _connector, _config = actor_app
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=token()))
    decision = PolicyDecision("deny", "owner_mismatch")

    def denied(_user, *, cached=False):
        if cached:
            return None
        raise PolicyError(decision)

    monkeypatch.setattr(client.app.state.policy, "actor_restriction_digest", denied)
    try:
        with pytest.raises(PolicyError, match="owner_mismatch"):
            bind_authenticated(client.app.state.policy, user)
    finally:
        clear_policy_context()


@pytest.mark.parametrize("invalid", ["malformed", "unknown_model", "unknown_field"])
@pytest.mark.parametrize("enabled", [False, True])
def test_configuration_failure_still_rejects_actual_startup(actor_app, invalid, enabled):
    from ai4ia_api.main import create_app

    client, _model, _token, calls, connector, config = actor_app
    settings = client.app.state.settings.model_copy(update={
        "group_policy_json": unavailable_config(config, invalid),
        "group_policy_enabled": enabled,
    })
    with pytest.raises((ValueError, PolicyError)):
        with TestClient(create_app(settings)):
            pytest.fail("Invalid policy reached application readiness.")
    assert not calls and not connector.calls


def test_restored_policy_still_admits_distinct_monitor_and_native_setup_in_one_app(actor_app):
    client, model, token, calls, connector, config = actor_app
    assert ready(client, model, token())
    client.app.state.settings.group_policy_json = "{"
    for subject in (ORDINARY, MONITOR, EVALUATOR, REALTIME):
        headers = authorization(token(subject))
        assert client.get("/api/sessions", headers=headers).status_code == 200
        assert_unavailable(client.get("/api/models", headers=headers))
        assert_unavailable(client.get("/api/tools", headers=headers))
    assert not calls and not connector.calls
    client.app.state.settings.group_policy_json = json.dumps(config)
    bearer = token()
    assert ready(client, model, bearer)
    response = client.post(
        "/api/chat", json=fresh_request(client, model, bearer), headers=authorization(bearer),
    )
    assert response.status_code == 200, response.text
    assert calls[0]["messages"] == [{"role": "user", "content": CANARY_SENTINEL}]
    exercise_setup(client, token(REALTIME))
    assert len(calls) == len(connector.calls) == len(connector.upstream.sent_text) == 1
    assert not connector.upstream.sent_bytes and connector.upstream.closed


async def test_invalid_policy_does_not_relax_signature_expiry_or_expected_owner(actor_app):
    client, model, token, calls, connector, _config = actor_app
    bearer = token(ORDINARY)
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=bearer))
    body = fresh_request(client, model, bearer)
    client.app.state.settings.group_policy_json = "{"
    for invalid_bearer in ("invalid-token", token(ORDINARY, exp=int(time.time()) - 1)):
        assert client.get("/api/sessions", headers=authorization(invalid_bearer)).status_code == 401
        assert client.delete(
            f"/api/sessions/{body['sessionId']}", headers=authorization(invalid_bearer),
        ).status_code == 401
    bind_authenticated(client.app.state.policy, user)
    try:
        with pytest.raises(PolicyError, match="owner_mismatch"):
            await require_policy(PolicyRequest("document.read"), owner_id="different-owner")
    finally:
        clear_policy_context()
    assert not calls and not connector.calls


@pytest.mark.parametrize("kind", ["legacy", "explicit"])
@pytest.mark.parametrize("profile,subject", [
    ("monitor-canary", MONITOR), ("authored-synthetic-evaluation", EVALUATOR),
    ("realtime-setup-canary", REALTIME),
])
async def test_failed_refresh_retains_cached_restricted_profile_and_exact_digest(actor_app, kind, profile, subject):
    client, model, token, calls, connector, config = actor_app
    configure(client, model, config, kind)
    policy = client.app.state.policy
    bearer = token(subject)
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=bearer))
    bind_authenticated(policy, user)
    previous = current_binding()
    assert previous.restricted_profile == profile
    assert (previous.actor_policy_digest is not None) is (kind == "explicit")
    client.app.state.settings.group_policy_json = "{"
    try:
        bind_authenticated(policy, user)
        binding = current_binding()
        assert binding.restricted_profile == previous.restricted_profile
        assert binding.actor_policy_digest == previous.actor_policy_digest
        assert binding.configuration_error == PolicyDecision("unavailable", "policy_unavailable")
        client.app.state.settings.group_policy_json = "{}"
        client.app.state.settings.group_policy_enabled = False
        with pytest.raises(PolicyError, match="policy_unavailable"):
            await binding.resolve()
    finally:
        clear_policy_context()
    assert not calls and not connector.calls


@pytest.mark.parametrize("subject", [ORDINARY, MONITOR, REALTIME])
def test_unavailable_policy_cannot_open_the_actual_relay(actor_app, subject):
    client, _model, token, calls, connector, config = actor_app
    realtime = token(REALTIME)
    target = setup_target(client, realtime)
    client.app.state.settings.group_policy_json = "{"
    bearer = token(subject)
    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect(
            target, subprotocols=["ai4ia-bearer", bearer], headers={"origin": ORIGIN},
        ):
            pytest.fail("Unavailable policy accepted a provider-backed relay.")
    assert denied.value.status_code == 503
    assert denied.value.json()["code"] == "policy_unavailable"
    assert not connector.calls and not calls
    client.app.state.settings.group_policy_json = json.dumps(config)
    exercise_setup(client, realtime)
    assert len(connector.calls) == len(connector.upstream.sent_text) == 1
    assert not connector.upstream.sent_bytes and connector.upstream.closed
