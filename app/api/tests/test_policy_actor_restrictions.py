"""One real app for ordinary callers and distinct roleless execution actors."""
from __future__ import annotations

import json
import hashlib
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ai4ia_api.auth.base import AuthCredentials
from ai4ia_api.catalog import load_catalog
from ai4ia_api.entitlements.models import Entitlement
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app
from ai4ia_api.policy.models import LIMIT_FIELDS, PolicyError, PolicyRequest, parse_policy_config, policy_digest
from ai4ia_api.realtime_canary import SETUP_INPUT
from ai4ia_api.request_constraints import CANARY_SENTINEL
from tests.conftest import make_settings
from tests.test_auth_entra import BARE_GUID, ISSUER, KID, TENANT, _new_keypair, _provider
from tests.test_policy_execution_profiles import EVALUATOR, MONITOR, provider_reply
from tests.test_realtime_canary_integration import ACTOR as REALTIME, ORIGIN, SetupConnector, setup_target
from tests.test_realtime_staged_api import GA_SETTINGS

ORDINARY = "00000000-0000-0000-0000-000000000099"
CLIENTS = {
    MONITOR: "00000000-0000-0000-0000-000000000101",
    EVALUATOR: "00000000-0000-0000-0000-000000000102",
    REALTIME: "00000000-0000-0000-0000-000000000103",
    ORDINARY: "00000000-0000-0000-0000-000000000199",
}
ROOT = Path(__file__).resolve().parents[3]


def authorization(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def actor_app(request, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))

    async def no_network(*_args, **_kwargs):
        pytest.fail("An offline actor control attempted a real HTTP transport.")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    model = next(
        item for item in load_catalog().models
        if item.conversational and item.api == "chat" and not item.reasoningEffortOptions
        and item.category in {"chat", "chat-fast"}
    )
    config = {}
    for name, subject, categories in (
        ("canaryActor", MONITOR, ["chat", "chat-fast"]),
        ("evaluationActor", EVALUATOR, [model.category]),
        ("realtimeCanaryActor", REALTIME, ["realtime"]),
    ):
        config[name] = {"tenantId": TENANT, "subject": subject}
        if getattr(request, "param", True):
            config[name]["restrictions"] = {
                "models": categories, "spend": {"requestsPerMinute": 50},
            }
    settings = make_settings(
        auth_provider="entra", entra_tenant_id=TENANT, entra_audience=BARE_GUID,
        applicationinsights_connection_string=None,
        group_policy_enabled=True, group_policy_json=json.dumps(config),
        session_deletion_enabled=True,
        realtime_enabled=True, realtime_protocol="ga", realtime_allowed_origins=ORIGIN,
        realtime_base_url="https://realtime-gateway.test/openai",
        realtime_gateway_api_key="synthetic-preview-key",
        **GA_SETTINGS,
    )
    private, jwks = _new_keypair(KID)
    calls = []

    def token(subject=MONITOR, **claims):
        now = int(time.time())
        return jwt.encode({
            "aud": BARE_GUID, "iss": ISSUER, "tid": TENANT, "oid": subject,
            "azp": CLIENTS[subject], "idtyp": "app",
            "iat": now, "exp": now + 3600, "roles": [], "scp": "", **claims,
        }, private, algorithm="RS256", headers={"kid": KID})

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        return provider_reply(request)

    app = create_app(settings)
    with TestClient(app) as client:
        app.state.auth_provider = _provider(jwks, audience=BARE_GUID)
        app.state.gateway = ModelGatewayClient(
            settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        app.state.realtime_connector = connector = SetupConnector()
        yield client, model, token, calls, connector, config


def observation_config(subject):
    from scripts.canaries.configuration import Configuration
    from scripts.canaries.contracts import stamp

    return Configuration(
        TENANT, CLIENTS[subject], subject, f"api://{BARE_GUID}",
        ORIGIN, "https://api.example.test", "55555555-5555-5555-5555-555555555555",
        stamp(datetime.now(timezone.utc) + timedelta(hours=1)), 4, 21600,
        True, True, True, subject == REALTIME,
    )


async def exercise_monitor(client, bearer):
    from scripts.canaries.contracts import Report, Run, stamp
    from scripts.canaries.identity import validate_api_token
    from scripts.canaries.monitor import chat
    from scripts.canaries.transport import Response

    requests = []

    class ApplicationTransport:
        async def request(self, method, url, *, token=None, body=None, **_):
            parsed = urlsplit(url)
            assert parsed.netloc == "web.example.test"
            requests.append((method, parsed.path))
            result = client.request(
                method, parsed.path + (f"?{parsed.query}" if parsed.query else ""),
                headers={**authorization(token), "Content-Type": "application/json"},
                content=body, follow_redirects=False,
            )
            return Response(
                result.status_code, result.content, 0.01,
                result.headers.get("content-type", "").split(";", 1)[0],
            )

    now = datetime.now(timezone.utc)
    config = observation_config(MONITOR)
    validate_api_token(bearer, config, now)
    report = Report(Run("owner/repo", 1, 200, 2, 1, "a" * 40), stamp(now))
    source = json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
    await chat(ApplicationTransport(), config, bearer, source, report)
    return report, requests


def exercise_setup(client, bearer):
    from scripts.canaries.identity import validate_api_token

    validate_api_token(bearer, observation_config(REALTIME), datetime.now(timezone.utc))
    response = client.get(
        "/api/canary/realtime-capabilities", headers=authorization(bearer),
    )
    assert response.status_code == 200, response.text
    capability = response.json()
    assert capability["ready"] is True
    target = (
        "/api/voice/live?provider=azure_openai"
        f"&model={capability['model']}&region={capability['region']}"
    )
    with client.websocket_connect(
        target, subprotocols=["ai4ia-bearer", bearer], headers={"origin": ORIGIN},
    ) as ws:
        assert dict(ws.extra_headers)[b"x-ai4ia-realtime-protocol"] == b"ga"
        assert json.loads(ws.receive_text())["type"] == "session.created"
        ws.send_text(SETUP_INPUT)
        assert json.loads(ws.receive_text())["type"] == "session.updated"


@pytest.mark.parametrize("actor_app", [False], indirect=True)
@pytest.mark.parametrize("categories,chat_ready,setup_ready", [
    (["realtime"], False, True),
    (["chat", "chat-fast"], True, False),
    (["chat", "chat-fast", "realtime"], True, False),
])
async def test_shared_defaults_cannot_admit_both_roleless_actors(
    actor_app, categories, chat_ready, setup_ready,
):
    client, _model, token, calls, connector, config = actor_app
    config["domains"] = {
        "models": {"default": {"allow": categories}},
        "tools": {"default": {"allow": []}},
        "documents": {"default": {"allow": []}},
    }
    config["spend"] = {"default": {"requestsPerMinute": 50}}
    client.app.state.settings.group_policy_json = json.dumps(config)
    report, requests = await exercise_monitor(client, token())
    assert (report.stages["model"].outcome == "pass") is chat_ready, report.document()
    assert len(calls) == int(chat_ready)
    if chat_ready:
        assert report.cleanup_safe
        assert client.get("/api/sessions", headers=authorization(token())).json() == []
        assert len(client.get(
            "/api/sessions/deletions", headers=authorization(token()),
        ).json()["items"]) == 1
    else:
        assert all(method == "GET" for method, _ in requests)
    setup = client.get(
        "/api/canary/realtime-capabilities", headers=authorization(token(REALTIME)),
    )
    assert setup.json()["ready"] is setup_ready
    if setup_ready:
        exercise_setup(client, token(REALTIME))
    assert len(connector.calls) == int(setup_ready)


async def test_explicit_restrictions_admit_both_actors_without_restricting_ordinary_users(actor_app):
    client, model, token, calls, connector, config = actor_app
    for subject in (MONITOR, REALTIME, EVALUATOR):
        principal = await client.app.state.auth_provider.authenticate(AuthCredentials(token=token(subject)))
        assert principal.policy_claims.roles == principal.policy_claims.groups == ()
        assert principal.policy_claims.roles_complete and principal.policy_claims.groups_complete
        assert not principal.policy_claims.groups_present

    ordinary = authorization(token(ORDINARY))
    snapshots = [client.get(path, headers=ordinary).json() for path in ("/api/models", "/api/tools")]
    client.app.state.settings.group_policy_json = "{}"
    assert snapshots == [client.get(path, headers=ordinary).json() for path in ("/api/models", "/api/tools")]
    client.app.state.settings.group_policy_json = json.dumps(config)
    assert len({item["category"] for item in snapshots[0]["models"]}) > 3
    assert any(item["available"] and item["selectable"] for item in snapshots[1]["tools"])

    for subject, categories in ((MONITOR, {"chat", "chat-fast"}), (REALTIME, {"realtime"})):
        headers = authorization(token(subject))
        models = client.get("/api/models", headers=headers).json()["models"]
        assert {item["category"] for item in models} == categories
        tools = client.get("/api/tools", headers=headers).json()["tools"]
        assert tools and not any(item["available"] or item["selectable"] for item in tools)

    report, requests = await exercise_monitor(client, token())
    assert all(report.stages[name].outcome == "pass" for name in (
        "platform", "auth", "catalog", "posture", "session", "gateway", "model", "persistence", "cleanup",
    )), report.document()
    assert report.cleanup_safe and report.usage_known
    assert report.estimated_micro_usd is not None
    assert len(calls) == 1
    sent = calls[0]
    sentinel = [{"role": "user", "content": CANARY_SENTINEL}]
    # Least-cost selection may choose either governed text protocol; the body
    # must be the sentinel-only envelope in the native shape the monitor reported.
    assert report.protocol in ("chat", "responses")
    assert ("input" in sent) is (report.protocol == "responses")
    if report.protocol == "responses":
        assert sent["input"] == sentinel and "messages" not in sent
        assert sent["max_output_tokens"] == 64 and sent["store"] is False
    else:
        assert sent["messages"] == sentinel
        assert sent.get("max_tokens", sent.get("max_completion_tokens")) == 64
    assert "tools" not in sent
    assert sum(method == "POST" and path == "/api/chat" for method, path in requests) == 1
    assert client.get("/api/sessions", headers=authorization(token())).json() == []
    assert len(client.get("/api/sessions/deletions", headers=authorization(token())).json()["items"]) == 1

    exercise_setup(client, token(REALTIME))
    assert len(connector.calls) == len(connector.upstream.sent_text) == 1
    native = connector.upstream.sent_text[0]["session"]
    assert native["type"] == "realtime" and native["output_modalities"] == ["text"]
    assert native["audio"]["input"]["turn_detection"] is None
    assert not native.get("tools") and not connector.upstream.sent_bytes
    assert connector.upstream.closed


def fresh_request(client, model, bearer, *, prompt=CANARY_SENTINEL, maximum=64):
    session = client.post(
        "/api/sessions", json={"model": model.id, "libraryDocumentIds": []},
        headers=authorization(bearer),
    )
    assert session.status_code == 201, session.text
    return {
        "sessionId": session.json()["id"], "content": prompt, "model": model.id,
        "stream": False, "allowTools": False, "allowAutomaticMemory": False,
        "requireFreshSession": True, "params": {"max_tokens": maximum},
    }


def ready(client, model, bearer, profile="monitor-canary"):
    result = client.get(
        "/api/execution-capabilities", params={"profile": profile, "model": model.id},
        headers=authorization(bearer),
    )
    assert result.status_code == 200, result.text
    return result.json()["ready"]


def test_documents_are_denied_only_to_exact_actors_in_the_same_app(actor_app):
    client, model, token, calls, connector, _config = actor_app
    for subject, expected in ((ORDINARY, 201), (MONITOR, 403), (REALTIME, 403), (EVALUATOR, 403)):
        headers = authorization(token(subject))
        body = fresh_request(client, model, token(subject))
        path = f"/api/sessions/{body['sessionId']}/documents"
        uploaded = client.post(
            path, files={"file": ("synthetic.txt", b"synthetic fixture", "text/plain")}, headers=headers,
        )
        assert uploaded.status_code == expected, uploaded.text
        read = client.get(path, headers=headers)
        assert read.status_code == (200 if subject == ORDINARY else 403)
        if subject == ORDINARY:
            assert len(read.json()) == 1
    assert not calls and not connector.calls


@pytest.mark.parametrize("subject,profile,prompt,maximum", [
    (MONITOR, "monitor-canary", CANARY_SENTINEL, 64),
    (EVALUATOR, "authored-synthetic-evaluation", "What is seven plus five?", 256),
])
def test_restricted_chat_profiles_still_use_the_real_one_dispatch_guard(
    actor_app, subject, profile, prompt, maximum,
):
    client, model, token, calls, connector, _config = actor_app
    bearer = token(subject)
    assert ready(client, model, bearer, profile)
    wrong = "authored-synthetic-evaluation" if subject == MONITOR else "monitor-canary"
    assert not ready(client, model, bearer, wrong)
    body = fresh_request(client, model, bearer, prompt=prompt, maximum=maximum)
    response = client.post("/api/chat", json=body, headers=authorization(bearer))
    assert response.status_code == 200, response.text
    assert len(calls) == 1 and calls[0]["messages"] == [{"role": "user", "content": prompt}]
    assert "tools" not in calls[0]
    assert client.post("/api/chat", json=body, headers=authorization(bearer)).status_code == 409
    assert len(calls) == 1 and not connector.calls


@pytest.mark.parametrize("change", ["tools", "memory", "fresh", "sentinel", "output", "guard"])
def test_monitor_request_reductions_remain_causal_with_actor_restrictions(actor_app, change):
    client, model, token, calls, _connector, _config = actor_app
    bearer = token()
    assert ready(client, model, bearer)
    body = fresh_request(client, model, bearer)
    changed = deepcopy(body)
    guard = client.app.state.canary_dispatch_guard
    if change in ("tools", "memory", "fresh"):
        field = {"tools": "allowTools", "memory": "allowAutomaticMemory", "fresh": "requireFreshSession"}[change]
        changed[field] = change != "fresh"
    elif change == "sentinel":
        changed["content"] = "Not the approved sentinel."
    elif change == "output":
        changed["params"]["max_tokens"] = 65
    else:
        client.app.state.canary_dispatch_guard = None
    response = client.post("/api/chat", json=changed, headers=authorization(bearer))
    assert response.status_code >= 400, response.text
    assert not calls
    client.app.state.canary_dispatch_guard = guard
    # A refused attempt may already have spent its v1 claim; never recycle it.
    control = fresh_request(client, model, bearer)
    assert client.post("/api/chat", json=control, headers=authorization(bearer)).status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize("rule", [
    {"allow": []},
    {"allow": ["chat", "chat-fast", "realtime"], "deny": ["chat", "chat-fast"]},
    {"allow": ["chat", "chat-fast", "realtime"], "restrict": ["realtime"]},
])
async def test_actor_models_intersect_global_allow_deny_and_restrict(actor_app, rule):
    client, model, token, calls, _connector, config = actor_app
    bearer = token()
    assert ready(client, model, bearer)
    config["domains"] = {"models": {"default": rule}}
    client.app.state.settings.group_policy_json = json.dumps(config)
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=bearer))
    resolved = await client.app.state.policy.resolve(user)
    assert resolved.domains["models"].allowed == frozenset()
    assert resolved.domains["models"].denied == frozenset(rule.get("deny", ()))
    if "restrict" in rule:
        assert resolved.domains["models"].restricted == frozenset(rule["restrict"]) & frozenset(
            config["canaryActor"]["restrictions"]["models"],
        )
    assert not ready(client, model, bearer)
    assert client.get("/api/models", headers=authorization(bearer)).json()["models"] == []
    response = client.post(
        "/api/chat", json=fresh_request(client, model, bearer), headers=authorization(bearer),
    )
    assert response.status_code >= 400 and not calls
    config["domains"]["models"]["default"] = {"allow": ["chat", "chat-fast", "realtime"]}
    client.app.state.settings.group_policy_json = json.dumps(config)
    assert ready(client, model, bearer)
    assert client.post(
        "/api/chat", json=fresh_request(client, model, bearer), headers=authorization(bearer),
    ).status_code == 200
    assert len(calls) == 1


async def test_all_actor_limits_intersect_owner_default_and_verified_claim_caps(actor_app):
    client, _model, token, _calls, _connector, config = actor_app
    policy = client.app.state.policy
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=token(roles=["capped"])))
    owner = user.internal_user_id
    config["spend"] = {
        "default": {name: 40 for name in LIMIT_FIELDS},
        "mappings": [{"claim": "roles", "value": "capped", "limits": {name: 30 for name in LIMIT_FIELDS}}],
    }
    config["canaryActor"]["restrictions"]["spend"] = {name: 20 for name in LIMIT_FIELDS}
    client.app.state.settings.group_policy_json = json.dumps(config)
    for owner_cap, expected in ((10, 10), (50, 20)):
        await policy.entitlements._store.put(Entitlement(
            id=owner, userId=owner, **{name: owner_cap for name in LIMIT_FIELDS},
        ))
        resolved = await policy.resolve(user)
        assert {name: getattr(resolved.limits, name) for name in LIMIT_FIELDS} == {
            name: expected for name in LIMIT_FIELDS
        }
    config["canaryActor"]["restrictions"]["spend"] = {name: 50 for name in LIMIT_FIELDS}
    client.app.state.settings.group_policy_json = json.dumps(config)
    resolved = await policy.resolve(user)
    assert all(getattr(resolved.limits, name) == 30 for name in LIMIT_FIELDS)
    roleless = await client.app.state.auth_provider.authenticate(AuthCredentials(token=token()))
    resolved = await policy.resolve(roleless)
    assert all(getattr(resolved.limits, name) == 40 for name in LIMIT_FIELDS)


@pytest.mark.parametrize("source", ["owner", "global", "actor"])
@pytest.mark.parametrize("restriction", [{"disabled": True}, {"requestsPerMinute": 0}])
async def test_actor_settings_never_remove_disabled_flags_or_zero_caps(actor_app, source, restriction):
    client, model, token, calls, _connector, config = actor_app
    bearer = token()
    assert ready(client, model, bearer)
    policy = client.app.state.policy
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=bearer))
    if source == "owner":
        await policy.entitlements._store.put(Entitlement(
            id=user.internal_user_id, userId=user.internal_user_id, **restriction,
        ))
    elif source == "global":
        config["spend"] = {"default": restriction}
    else:
        config["canaryActor"]["restrictions"]["spend"].update(restriction)
    client.app.state.settings.group_policy_json = json.dumps(config)
    assert not ready(client, model, bearer)
    response = client.post(
        "/api/chat", json=fresh_request(client, model, bearer), headers=authorization(bearer),
    )
    assert response.status_code in (403, 429) and not calls


async def test_finite_actor_spend_does_not_turn_owner_store_outage_into_authority(actor_app, monkeypatch):
    client, model, token, calls, _connector, _config = actor_app
    bearer = token()
    assert ready(client, model, bearer)

    async def unavailable(_owner):
        raise RuntimeError("Synthetic entitlement read failure.")

    monkeypatch.setattr(client.app.state.entitlements._store, "get_strict", unavailable)
    assert not ready(client, model, bearer)
    response = client.post(
        "/api/chat", json=fresh_request(client, model, bearer), headers=authorization(bearer),
    )
    assert response.status_code == 503 and not calls


@pytest.mark.parametrize("claims", [
    {"roles": None}, {"roles": ["duplicate", "duplicate"]}, {"roles": "admin"},
    {"groups": None}, {"groups": ["not-a-group-guid"]},
    {"hasgroups": True}, {"_claim_names": {"groups": "source"}},
    {"_claim_sources": {"source": {"endpoint": "https://forbidden.example.test"}}},
])
def test_actor_restrictions_cannot_make_invalid_or_incomplete_signed_claims_valid(actor_app, claims):
    client, model, token, calls, _connector, _config = actor_app
    assert ready(client, model, token())
    bearer = token(**claims)
    assert not ready(client, model, bearer)
    assert client.get("/api/models", headers=authorization(bearer)).json()["models"] == []
    tools = client.get("/api/tools", headers=authorization(bearer)).json()["tools"]
    assert tools and not any(item["available"] for item in tools)
    response = client.post(
        "/api/chat", json=fresh_request(client, model, bearer), headers=authorization(bearer),
    )
    assert response.status_code >= 400 and not calls


@pytest.mark.parametrize("domain,value", [
    ("models", "chat"), ("tools", "calculator"), ("documents", "read"),
    ("zones", "global"), ("admin", "admin.usage.read"), ("publication", "review"),
])
async def test_actor_overlay_preserves_incomplete_negative_claim_evidence(actor_app, domain, value):
    client, model, token, _calls, _connector, config = actor_app
    config["domains"] = {domain: {
        "default": {"allow": ["chat", "chat-fast", "realtime"] if domain == "models" else []},
        "mappings": [{
            "claim": "groups", "value": "11111111-2222-3333-4444-555555555555", "deny": [value],
        }],
    }}
    if domain == "zones":
        config["domains"][domain]["default"]["allow"] = ["global"]
    client.app.state.settings.group_policy_json = json.dumps(config)
    assert ready(client, model, token(groups=[]))
    assert not ready(client, model, token())
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=token()))
    resolved = await client.app.state.policy.resolve(user)
    assert resolved.domains[domain].invalid
    assert resolved.domains["models"].invalid


@pytest.mark.parametrize("privilege", ["bootstrap", "admin_role", "mapped_admin", "publisher", "reviewer"])
async def test_actor_restrictions_never_hide_underlying_admin_or_publisher_privileges(actor_app, privilege):
    client, model, token, calls, _connector, config = actor_app
    bearer = token()
    assert ready(client, model, bearer)
    if privilege == "bootstrap":
        client.app.state.settings.admin_subjects = MONITOR
    elif privilege == "admin_role":
        bearer = token(roles=["admin"])
    else:
        domain = "admin" if privilege == "mapped_admin" else "publication"
        value = "admin.usage.read" if domain == "admin" else "submit" if privilege == "publisher" else "review"
        config["domains"] = {domain: {"default": {"allow": [value]}}}
        if domain == "admin":
            config["adminCeiling"] = [value]
        client.app.state.settings.group_policy_json = json.dumps(config)
        user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=bearer))
        resolved = await client.app.state.policy.resolve(user)
        assert value in resolved.domains[domain].allowed
    assert not ready(client, model, bearer)
    response = client.post(
        "/api/chat", json=fresh_request(client, model, bearer), headers=authorization(bearer),
    )
    assert response.status_code >= 400 and not calls


def test_ordinary_mapped_admin_and_publication_behavior_is_unchanged(actor_app):
    client, _model, token, calls, connector, config = actor_app
    config.update({
        "domains": {
            "admin": {"default": {"allow": []}, "mappings": [{
                "claim": "roles", "value": "ordinary.operator", "allow": ["admin.usage.read"],
            }]},
            "publication": {"default": {"allow": ["consume"]}, "mappings": [{
                "claim": "roles", "value": "ordinary.publisher", "allow": ["submit", "review"],
            }]},
        },
        "adminCeiling": ["admin.usage.read"],
    })
    client.app.state.settings.asset_publishing_enabled = True
    headers = authorization(token(ORDINARY, roles=["ordinary.operator", "ordinary.publisher"]))
    results = []
    for value in (config, {key: item for key, item in config.items() if not key.endswith("Actor")}):
        client.app.state.settings.group_policy_json = json.dumps(value)
        admin = client.get("/api/admin/usage/summary", headers=headers)
        publication = client.get("/api/publications/capabilities", headers=headers)
        assert admin.status_code == publication.status_code == 200
        assert set(publication.json()["actions"]) == {"submit", "review", "consume"}
        assert client.get(
            "/api/admin/usage/summary", headers=authorization(token(ORDINARY)),
        ).status_code == 403
        summary = admin.json()
        assert summary["fromTime"] < summary["toTime"]
        results.append((
            {key: value for key, value in summary.items() if key not in {"fromTime", "toTime"}},
            publication.json(),
        ))
    assert results[0] == results[1]
    assert not calls and not connector.calls


@pytest.mark.parametrize("block", [
    {}, [], True, {"spend": {"requestsPerMinute": 1}},
    {"models": ["chat"]}, {"models": None, "spend": {"requestsPerMinute": 1}},
    {"models": "chat", "spend": {"requestsPerMinute": 1}},
    {"models": ["chat", "chat"], "spend": {"requestsPerMinute": 1}},
    {"models": ["chat*"], "spend": {"requestsPerMinute": 1}},
    {"models": [" chat"], "spend": {"requestsPerMinute": 1}},
    {"models": [7], "spend": {"requestsPerMinute": 1}},
    {"models": ["chat"], "spend": {}},
    {"models": ["chat"], "spend": {"disabled": True}},
    {"models": ["chat"], "spend": {"computeExecutionsPerDay": 1}},
    {"models": ["chat"], "spend": {"requestsPerMinute": -1}},
    {"models": ["chat"], "spend": {"requestsPerMinute": True}},
    {"models": ["chat"], "spend": {"requestsPerMinute": "1"}},
    {"models": ["chat"], "spend": {"requestsPerMinute": 1.5}},
    {"models": ["chat"], "spend": {"requestsPerMinute": 1, "hardUsdCap": 1}},
    {"models": ["chat"], "spend": {"requestsPerMinute": 1}, "tools": ["calculator"]},
    {"models": ["chat"], "spend": {"requestsPerMinute": 1}, "admin": []},
])
def test_actor_restriction_schema_has_no_permissive_or_unknown_fallback(block):
    marker = {"tenantId": TENANT, "subject": MONITOR}
    good = {"models": ["chat"], "spend": {"requestsPerMinute": 1}}
    assert parse_policy_config(json.dumps({"canaryActor": {**marker, "restrictions": good}}))
    with pytest.raises(ValueError):
        parse_policy_config(json.dumps({"canaryActor": {**marker, "restrictions": block}}))


def test_duplicate_actor_restriction_json_keys_are_rejected():
    raw = (
        '{"canaryActor":{"tenantId":"synthetic","subject":"synthetic","restrictions":'
        '{"models":["chat"],"models":["realtime"],"spend":{"requestsPerMinute":1}}}}'
    )
    with pytest.raises(ValueError, match="Duplicate"):
        parse_policy_config(raw)


@pytest.mark.parametrize("marker", ["canaryActor", "evaluationActor", "realtimeCanaryActor"])
@pytest.mark.parametrize("setting", ["entitlements_enabled", "usage_metering_enabled"])
def test_actor_only_spend_requires_existing_enforcement_at_actual_factory_startup(marker, setting):
    config = {marker: {"tenantId": TENANT, "subject": MONITOR, "restrictions": {
        "models": ["chat"], "spend": {"requestsPerMinute": 1},
    }}}
    settings = make_settings(
        group_policy_enabled=True, group_policy_json=json.dumps(config),
        applicationinsights_connection_string=None,
    )
    create_app(settings)
    setattr(settings, setting, False)
    with pytest.raises(RuntimeError, match="soft entitlements and metering"):
        create_app(settings)


@pytest.mark.parametrize("marker", ["canaryActor", "evaluationActor", "realtimeCanaryActor"])
@pytest.mark.parametrize("enabled", [False, True])
def test_unknown_actor_category_fails_actual_factory_even_when_policy_paused(marker, enabled):
    config = {marker: {"tenantId": TENANT, "subject": MONITOR, "restrictions": {
        "models": ["not-a-catalog-category"], "spend": {"requestsPerMinute": 1},
    }}}
    settings = make_settings(
        group_policy_enabled=enabled, group_policy_json=json.dumps(config),
        applicationinsights_connection_string=None,
    )
    with pytest.raises(PolicyError, match="policy_unavailable"):
        with TestClient(create_app(settings)):
            pytest.fail("Unknown actor model categories reached application readiness.")
    config[marker]["restrictions"]["models"] = [load_catalog().models[0].category]
    settings.group_policy_json = json.dumps(config)
    with TestClient(create_app(settings)) as client:
        assert client.app.state.policy._configuration().actor_for({
            "canaryActor": "monitor-canary", "evaluationActor": "authored-synthetic-evaluation",
            "realtimeCanaryActor": "realtime-setup-canary",
        }[marker]).restrictions is not None


@pytest.mark.parametrize("explicit_null", [False, True])
def test_omitted_actor_restrictions_preserve_legacy_configuration_digest(explicit_null):
    marker = {"tenantId": TENANT, "subject": MONITOR}
    config = parse_policy_config(json.dumps({
        "canaryActor": {**marker, **({"restrictions": None} if explicit_null else {})},
    }))
    legacy = {
        "version": 1, "domains": {}, "spend": None, "adminCeiling": [],
        "canaryActor": marker, "evaluationActor": None, "realtimeCanaryActor": None,
    }
    expected = hashlib.sha256(json.dumps(
        legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("ascii")).hexdigest()
    assert policy_digest(config) == expected
    assert config.canaryActor.restrictions is None


@pytest.mark.parametrize("change", ["models", "spend"])
def test_explicit_actor_restrictions_are_in_the_existing_policy_digest(change):
    raw = {"canaryActor": {"tenantId": TENANT, "subject": MONITOR, "restrictions": {
        "models": ["chat"], "spend": {"requestsPerMinute": 2},
    }}}
    before = policy_digest(parse_policy_config(json.dumps(raw)))
    if change == "models":
        raw["canaryActor"]["restrictions"]["models"] = ["chat-fast"]
    else:
        raw["canaryActor"]["restrictions"]["spend"]["requestsPerMinute"] = 1
    assert policy_digest(parse_policy_config(json.dumps(raw))) != before


@pytest.mark.parametrize("change", ["marker", "block", "models", "spend", "disabled"])
@pytest.mark.parametrize("marker,subject,surface", [
    ("canaryActor", MONITOR, "chat"), ("evaluationActor", EVALUATOR, "chat"),
    ("realtimeCanaryActor", REALTIME, "realtime"),
])
async def test_bound_actor_profile_never_restores_ordinary_authority_after_configuration_change(
    actor_app, change, marker, subject, surface,
):
    from ai4ia_api.policy.context import (
        bind_authenticated, clear_policy_context, current_binding, model_allowed, require_policy, tool_allowed,
    )
    from ai4ia_api.policy.dispatch import authorize_dispatch

    client, model, token, calls, connector, config = actor_app
    service = client.app.state.policy
    if surface == "realtime":
        model = next(item for item in client.app.state.catalog.models if item.category == "realtime")
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=token(subject)))
    option = model.options[0]
    bind_authenticated(service, user)
    try:
        binding = current_binding()
        assert model_allowed(model.category, option) and not tool_allowed("calculator")
        await require_policy(PolicyRequest("model.invoke", model_id=model.id, deployment=option))
        changed = deepcopy(config)
        if change == "marker":
            del changed[marker]
        elif change == "block":
            del changed[marker]["restrictions"]
        elif change == "models":
            changed[marker]["restrictions"]["models"].append("reasoning")
        elif change == "spend":
            changed[marker]["restrictions"]["spend"]["requestsPerMinute"] = 49
        else:
            client.app.state.settings.group_policy_enabled = False
        client.app.state.settings.group_policy_json = json.dumps(changed)
        assert not model_allowed(model.category, option)
        assert not tool_allowed("calculator")
        with pytest.raises(PolicyError):
            await binding.resolve()
        with pytest.raises(PolicyError):
            await require_policy(PolicyRequest("document.read"))
        with pytest.raises(PolicyError):
            await authorize_dispatch(
                surface, deployment=option.deploymentName, service=service,
                expected_owner=user.internal_user_id,
            )
        client.app.state.settings.group_policy_enabled = True
        client.app.state.settings.group_policy_json = json.dumps(config)
        bind_authenticated(service, user)
        await require_policy(PolicyRequest("model.invoke", model_id=model.id, deployment=option))
    finally:
        clear_policy_context()
    assert not calls and not connector.calls


@pytest.mark.parametrize("claim,value", [
    ("roles", "restricted"), ("groups", "11111111-2222-3333-4444-555555555555"),
])
@pytest.mark.parametrize("rule", [{"deny": ["chat", "chat-fast"]}, {"restrict": ["realtime"]}])
def test_matched_claim_restrictions_still_intersect_actor_models(actor_app, claim, value, rule):
    client, model, token, calls, _connector, config = actor_app
    config["domains"] = {"models": {
        "default": {"allow": ["chat", "chat-fast", "realtime"]},
        "mappings": [{"claim": claim, "value": value, **rule}],
    }}
    client.app.state.settings.group_policy_json = json.dumps(config)
    allowed = token(groups=[])
    assert ready(client, model, allowed)
    claims = {"groups": [], "roles": []}
    claims[claim] = [value]
    denied = token(**claims)
    assert not ready(client, model, denied)
    assert client.get("/api/models", headers=authorization(denied)).json()["models"] == []
    result = client.post(
        "/api/chat", json=fresh_request(client, model, denied), headers=authorization(denied),
    )
    assert result.status_code >= 400 and not calls
    assert client.post(
        "/api/chat", json=fresh_request(client, model, allowed), headers=authorization(allowed),
    ).status_code == 200
    assert len(calls) == 1


async def test_direct_policy_snapshot_readers_refuse_configured_actors_when_paused(actor_app):
    client, model, token, _calls, _connector, _config = actor_app
    policy = client.app.state.policy
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=token()))
    ordinary = await client.app.state.auth_provider.authenticate(AuthCredentials(token=token(ORDINARY)))
    assert policy.allows_model_snapshot(user, model.category, model.options[0])
    assert policy.allows_tool_snapshot(ordinary, "calculator")
    policy.settings.group_policy_enabled = False
    assert not policy.allows_model_snapshot(user, model.category, model.options[0])
    assert not policy.allows_tool_snapshot(user, "calculator")
    assert policy.allows_model_snapshot(ordinary, model.category, model.options[0])
    assert policy.allows_tool_snapshot(ordinary, "calculator")


def test_standing_model_selection_cannot_bypass_actor_catalog_restrictions(actor_app):
    client, model, token, calls, _connector, _config = actor_app
    outside = next(
        item for item in client.app.state.catalog.models
        if item.conversational and item.api == "chat" and item.category not in {"chat", "chat-fast"}
    )
    for subject, selected, expected in ((MONITOR, outside, 400), (ORDINARY, outside, 200), (MONITOR, model, 200)):
        bearer = token(subject)
        request = fresh_request(client, selected, bearer)
        del request["model"]
        before = len(calls)
        result = client.post("/api/chat", json=request, headers=authorization(bearer))
        assert result.status_code == expected, result.text
        assert len(calls) - before == int(expected == 200)


async def test_actor_expiry_stops_snapshots_and_current_authorization(actor_app, monkeypatch):
    from ai4ia_api.policy.context import bind_authenticated, clear_policy_context, model_allowed, require_policy

    client, model, token, _calls, _connector, _config = actor_app
    service = client.app.state.policy
    expiry = int(time.time()) + 3600
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=token(exp=expiry)))
    bind_authenticated(service, user)
    try:
        request = PolicyRequest("model.invoke", model_id=model.id, deployment=model.options[0])
        assert model_allowed(model.category, model.options[0])
        await require_policy(request)
        monkeypatch.setattr("ai4ia_api.policy.service.time.time", lambda: expiry)
        assert not model_allowed(model.category, model.options[0])
        with pytest.raises(PolicyError, match="reauthentication_required"):
            await require_policy(request)
    finally:
        clear_policy_context()


@pytest.mark.parametrize("change", [None, "models", "spend", "marker", "owner_limit"])
def test_fresh_dispatch_rereads_restrictions_after_real_guard_consumes_its_one_shot(actor_app, change):
    client, model, token, calls, _connector, config = actor_app
    bearer = token()
    assert ready(client, model, bearer)
    guard = client.app.state.canary_dispatch_guard
    consumed = []

    async def changing_guard(owner, deployment, payload):
        allowed = await guard(owner, deployment, payload)
        consumed.append(allowed)
        if change == "models":
            config["canaryActor"]["restrictions"]["models"].append("realtime")
        elif change == "spend":
            config["canaryActor"]["restrictions"]["spend"]["requestsPerMinute"] = 49
        elif change == "marker":
            del config["canaryActor"]
        elif change == "owner_limit":
            await client.app.state.entitlements._store.put(Entitlement(
                id=owner, userId=owner, requestsPerMinute=0,
            ))
        client.app.state.settings.group_policy_json = json.dumps(config)
        return allowed

    client.app.state.canary_dispatch_guard = changing_guard
    result = client.post(
        "/api/chat", json=fresh_request(client, model, bearer), headers=authorization(bearer),
    )
    assert consumed == [True]
    assert (result.status_code == 200) is (change is None), result.text
    assert len(calls) == int(change is None)


@pytest.mark.parametrize("change", [None, "owner", "generation", "claim"])
def test_actual_actor_dispatch_retains_owner_and_claimed_generation_binding(actor_app, monkeypatch, change):
    client, model, token, calls, _connector, _config = actor_app
    bearer = token()
    assert ready(client, model, bearer)
    guard = client.app.state.canary_dispatch_guard
    repo = client.app.state.session_repo
    get_session = repo.get_session
    checked = []

    async def guarded(owner, deployment, payload):
        async def changed_record(read_owner, session_id):
            current = await get_session(read_owner, session_id)
            assert current.freshTurnClaimed and current.deletionEpoch
            if change == "generation":
                return current.model_copy(update={"deletionEpoch": "different-synthetic-generation"})
            if change == "claim":
                return current.model_copy(update={"freshTurnClaimed": False})
            return current

        monkeypatch.setattr(repo, "get_session", changed_record)
        allowed = await guard("different-owner" if change == "owner" else owner, deployment, payload)
        checked.append(allowed)
        monkeypatch.setattr(repo, "get_session", get_session)
        return allowed

    client.app.state.canary_dispatch_guard = guarded
    result = client.post(
        "/api/chat", json=fresh_request(client, model, bearer), headers=authorization(bearer),
    )
    assert checked == [change is None]
    assert (result.status_code == 200) is (change is None), result.text
    assert len(calls) == int(change is None)


@pytest.mark.parametrize("payload", [
    '{"type":"response.create"}',
    '{"type":"input_audio_buffer.append","audio":"AAAA"}',
    '{"type":"session.update","session":{"instructions":"unapproved"}}',
    b"synthetic audio",
])
def test_realtime_actor_restrictions_do_not_weaken_setup_only_native_relay(actor_app, payload):
    client, _model, token, calls, connector, _config = actor_app
    bearer = token(REALTIME)
    with client.websocket_connect(
        setup_target(client, bearer), subprotocols=["ai4ia-bearer", bearer], headers={"origin": ORIGIN},
    ) as ws:
        assert json.loads(ws.receive_text())["type"] == "session.created"
        if isinstance(payload, bytes):
            ws.send_bytes(payload)
        else:
            ws.send_text(payload)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert len(connector.calls) == 1 and connector.upstream.closed
    assert not connector.upstream.sent_text and not connector.upstream.sent_bytes and not calls
    client.app.state.realtime_connector = control = SetupConnector()
    exercise_setup(client, bearer)
    assert len(control.calls) == len(control.upstream.sent_text) == 1


@pytest.mark.parametrize("change", [None, "marker", "block", "models", "policy_disabled"])
def test_realtime_actor_rechecks_changed_restrictions_before_native_setup_send(actor_app, change):
    client, _model, token, _calls, connector, config = actor_app
    bearer = token(REALTIME)
    with client.websocket_connect(
        setup_target(client, bearer), subprotocols=["ai4ia-bearer", bearer], headers={"origin": ORIGIN},
    ) as ws:
        assert json.loads(ws.receive_text())["type"] == "session.created"
        if change == "marker":
            del config["realtimeCanaryActor"]
        elif change == "block":
            del config["realtimeCanaryActor"]["restrictions"]
        elif change == "models":
            config["realtimeCanaryActor"]["restrictions"]["models"].append("chat")
        elif change == "policy_disabled":
            client.app.state.settings.group_policy_enabled = False
        client.app.state.settings.group_policy_json = json.dumps(config)
        ws.send_text(SETUP_INPUT)
        if change is None:
            assert json.loads(ws.receive_text())["type"] == "session.updated"
        else:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
    assert len(connector.calls) == 1 and connector.upstream.closed
    assert len(connector.upstream.sent_text) == int(change is None)


def test_real_ordinary_tool_and_memory_execution_survive_actor_only_configuration(actor_app, monkeypatch):
    from ai4ia_api.memory.in_memory import InMemoryVectorStore
    from ai4ia_api.memory.service import MemoryService
    from tests.test_memory_preference import RecordingEmbedder

    client, model, token, calls, _connector, _config = actor_app
    embedder = RecordingEmbedder()
    client.app.state.memory = MemoryService(
        store=InMemoryVectorStore(), embedder=embedder, min_chars_to_store=8,
    )
    executor = client.app.state.tool_executor
    execute = executor.execute
    executed = []

    async def recording_execute(name, args, ctx):
        result = await execute(name, args, ctx)
        executed.append((name, result))
        return result

    monkeypatch.setattr(executor, "execute", recording_execute)
    ordinary = token(ORDINARY)
    request = fresh_request(client, model, ordinary, prompt="/calculator 6*7")
    request.update(allowTools=True, requireFreshSession=False)
    result = client.post("/api/chat", json=request, headers=authorization(ordinary))
    assert result.status_code == 200, result.text
    assert "42" in result.json()["message"]["content"]
    assert len(executed) == 1 and executed[0][0] == "calculator"
    assert not calls

    # The same real handler remains refused for the dedicated actor.
    monitor = token()
    request = fresh_request(client, model, monitor, prompt="/calculator 6*7")
    request.update(allowTools=True, requireFreshSession=False)
    client.post("/api/chat", json=request, headers=authorization(monitor))
    assert len(executed) == 1 and not calls

    request = fresh_request(client, model, ordinary, prompt="A durable non-sensitive synthetic fact.")
    request.update(allowAutomaticMemory=True, requireFreshSession=False)
    result = client.post("/api/chat", json=request, headers=authorization(ordinary))
    assert result.status_code == 200, result.text
    assert embedder.calls and len(calls) == 1
    embedder.calls.clear()
    request = fresh_request(client, model, monitor)
    result = client.post("/api/chat", json=request, headers=authorization(monitor))
    assert result.status_code == 200, result.text
    assert not embedder.calls and len(executed) == 1 and len(calls) == 2
    assert calls[-1]["messages"] == [{"role": "user", "content": CANARY_SENTINEL}]
    assert "tools" not in calls[-1]


@pytest.mark.parametrize("change", [
    "policy_disabled", "realtime_disabled", "preview", "guard", "monitor", "query_tools",
])
def test_actor_restrictions_do_not_activate_disabled_or_wrong_realtime_paths(actor_app, change):
    from ai4ia_api.realtime_protocol import RealtimeProtocol

    client, _model, token, calls, _connector, _config = actor_app
    bearer = token(REALTIME)
    target = setup_target(client, bearer)
    exercise_setup(client, bearer)
    client.app.state.realtime_connector = refused = SetupConnector()
    if change == "policy_disabled":
        client.app.state.settings.group_policy_enabled = False
    elif change == "realtime_disabled":
        client.app.state.settings.realtime_enabled = False
    elif change == "preview":
        client.app.state.settings.realtime_protocol = RealtimeProtocol.preview
    elif change == "guard":
        client.app.state.realtime_canary_dispatch_guard = None
    elif change == "monitor":
        bearer = token()
    else:
        target += "&tools=calculator"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            target + "&profile=realtime-setup-canary",
            subprotocols=["ai4ia-bearer", bearer], headers={"origin": ORIGIN},
        ) as ws:
            ws.send_text(SETUP_INPUT)
            ws.receive_text()
    assert not refused.calls and not calls


def test_caller_profile_labels_do_not_select_actor_authority(actor_app):
    client, model, token, calls, connector, _config = actor_app
    ordinary = token(ORDINARY)
    assert not ready(client, model, ordinary)
    assert not ready(client, model, ordinary, "authored-synthetic-evaluation")
    assert not client.get(
        "/api/canary/realtime-capabilities", headers=authorization(ordinary),
    ).json()["ready"]
    # Ordinary chat does not acquire a canary guard merely from a caller label.
    request = fresh_request(client, model, ordinary, prompt="An ordinary synthetic turn.")
    request.update(requireFreshSession=False)
    assert client.post(
        "/api/chat?profile=realtime-setup-canary", json=request, headers=authorization(ordinary),
    ).status_code == 200
    for subject in (MONITOR, REALTIME):
        bearer = token(subject)
        request = fresh_request(client, model, bearer, prompt="An authored non-sentinel turn.", maximum=256)
        result = client.post(
            "/api/chat?profile=authored-synthetic-evaluation", json=request, headers=authorization(bearer),
        )
        assert result.status_code >= 400, result.text
    assert len(calls) == 1 and not connector.calls


@pytest.mark.parametrize("claims", [
    {"roles": ["permissive-role"]}, {"scp": "scope"},
    {"azp": CLIENTS[REALTIME]}, {"appid": CLIENTS[REALTIME]}, {"oid": REALTIME},
    {"tid": "00000000-0000-0000-0000-000000000077"},
    {"aud": "00000000-0000-0000-0000-000000000077"},
    {"iss": "https://wrong-issuer.example.test"}, {"idtyp": "user"},
    {"email": "synthetic@example.test"},
])
def test_monitor_token_validator_remains_strict_with_actor_restrictions(actor_app, claims):
    from scripts.canaries.contracts import CanaryError
    from scripts.canaries.identity import validate_api_token

    _client, _model, token, calls, connector, _config = actor_app
    now = datetime.now(timezone.utc)
    validate_api_token(token(), observation_config(MONITOR), now)
    with pytest.raises(CanaryError, match="identity_rejected"):
        validate_api_token(token(**claims), observation_config(MONITOR), now)
    assert not calls and not connector.calls


@pytest.mark.parametrize("claims", [
    {"tid": "00000000-0000-0000-0000-000000000077"},
    {"aud": "00000000-0000-0000-0000-000000000077"},
    {"iss": "https://wrong-issuer.example.test"}, {"exp": 1},
])
def test_signed_claims_still_cross_actual_entra_validation(actor_app, claims):
    client, model, token, calls, connector, _config = actor_app
    assert ready(client, model, token())
    result = client.get("/api/models", headers=authorization(token(**claims)))
    assert result.status_code == 401
    assert not calls and not connector.calls


@pytest.mark.parametrize("marker", ["canaryActor", "evaluationActor", "realtimeCanaryActor"])
async def test_restriction_identity_matching_requires_provider_tenant_subject_and_owner(actor_app, marker):
    client, _model, token, _calls, _connector, config = actor_app
    profile = {
        "canaryActor": "monitor-canary", "evaluationActor": "authored-synthetic-evaluation",
        "realtimeCanaryActor": "realtime-setup-canary",
    }[marker]
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=token(config[marker]["subject"])))
    policy = client.app.state.policy
    parsed = parse_policy_config(json.dumps(config))
    assert policy._actor_restrictions(parsed, user) == parsed.actor_for(profile).restrictions
    for field, value in (
        ("provider", "dev"), ("tenant_id", "different-tenant"),
        ("subject", ORDINARY), ("internal_user_id", "different-owner"),
    ):
        changed = user.model_copy(update={field: value})
        assert policy._actor_restrictions(parsed, changed) is None
    assert policy._actor_restrictions(parsed, None) is None


@pytest.mark.parametrize("categories", [[], sorted({entry.category for entry in load_catalog().models})])
def test_empty_or_unbounded_actor_models_never_fall_back_to_canary_authority(actor_app, categories):
    client, model, token, calls, _connector, config = actor_app
    bearer = token()
    assert ready(client, model, bearer)
    config["canaryActor"]["restrictions"]["models"] = categories
    client.app.state.settings.group_policy_json = json.dumps(config)
    assert not ready(client, model, bearer)
    result = client.post(
        "/api/chat", json=fresh_request(client, model, bearer), headers=authorization(bearer),
    )
    assert result.status_code >= 400 and not calls


def test_invalid_changed_config_cannot_reuse_a_previously_valid_actor_snapshot(actor_app):
    client, model, token, _calls, _connector, config = actor_app
    bearer = token()
    assert ready(client, model, bearer)
    changed = deepcopy(config)
    changed["canaryActor"]["restrictions"]["models"] = ["unknown-category"]
    client.app.state.settings.group_policy_json = json.dumps(changed)
    assert client.get("/api/models", headers=authorization(bearer)).status_code == 503
    assert client.get("/api/tools", headers=authorization(bearer)).status_code == 503
    client.app.state.settings.group_policy_json = json.dumps(config)
    assert ready(client, model, bearer)


@pytest.mark.parametrize("paused_startup", [False, True])
def test_paused_policy_keeps_explicit_actor_snapshots_restricted_but_ordinary_users_unchanged(actor_app, paused_startup):
    client, _model, token, calls, connector, _config = actor_app
    ordinary = authorization(token(ORDINARY))
    baseline = [client.get(path, headers=ordinary).json() for path in ("/api/models", "/api/tools")]
    client.app.state.settings.group_policy_enabled = False

    def check(paused):
        for subject in (MONITOR, EVALUATOR, REALTIME):
            headers = authorization(token(subject))
            assert paused.get("/api/models", headers=headers).json()["models"] == []
            tools = paused.get("/api/tools", headers=headers).json()["tools"]
            assert tools and not any(item["available"] or item["selectable"] for item in tools)
        assert baseline == [paused.get(path, headers=ordinary).json() for path in ("/api/models", "/api/tools")]

    if paused_startup:
        with TestClient(create_app(client.app.state.settings)) as paused:
            paused.app.state.auth_provider = client.app.state.auth_provider
            check(paused)
    else:
        check(client)
    assert not calls and not connector.calls


@pytest.mark.parametrize("actor_app", [False], indirect=True)
async def test_bound_legacy_actor_removal_cannot_restore_an_ordinary_profile(actor_app):
    from ai4ia_api.policy.context import bind_authenticated, clear_policy_context, current_binding

    client, model, token, _calls, _connector, config = actor_app
    config.update({
        "domains": {
            "models": {"default": {"allow": [model.category]}},
            "tools": {"default": {"allow": []}},
            "documents": {"default": {"allow": []}},
        },
        "spend": {"default": {"requestsPerMinute": 50}},
    })
    client.app.state.settings.group_policy_json = json.dumps(config)
    bearer = token()
    assert ready(client, model, bearer)
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=bearer))
    bind_authenticated(client.app.state.policy, user)
    try:
        binding = current_binding()
        await binding.resolve()
        assert binding.actor_policy_digest is None
        del config["canaryActor"]
        client.app.state.settings.group_policy_json = json.dumps(config)
        with pytest.raises(PolicyError, match="canary_policy_unconfigured"):
            await binding.resolve()
    finally:
        clear_policy_context()


@pytest.mark.parametrize("profile", [
    "monitor-canary", "authored-synthetic-evaluation", "realtime-setup-canary",
])
async def test_actor_only_restrictions_never_construct_unattended_claim_authority(actor_app, profile):
    client, _model, _token, _calls, _connector, _config = actor_app
    policy = client.app.state.policy
    owner = policy.profile_owner(profile)
    restricted = await policy.resolve_unattended(owner)
    decision = await policy.authorize(restricted, PolicyRequest("tool.invoke", tool_name="calculator"))
    assert not decision.allowed
    assert decision.reason in ("reauthentication_required", "canary_policy_incompatible")
    ordinary = await policy.resolve_unattended("ordinary-unattended-owner")
    assert (await policy.authorize(ordinary, PolicyRequest("tool.invoke", tool_name="calculator"))).allowed
