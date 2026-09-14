"""The live driver against signed auth, real policy/factories and native HTTP fixtures."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest

from scripts.evaluations import live
from scripts.evaluations.contracts import ROOT
from scripts.evaluations.live_contracts import LiveConfig, authored_prompt, bind_token, load_live_dataset, source_documents
from scripts.evaluations.live_http import ApiClient, Budget, HttpResult
from scripts.evaluations.offline import offline_environment

CLIENT = "00000000-0000-4000-8000-000000000010"
DEPLOY = "00000000-0000-4000-8000-000000000011"
EVALUATOR = "00000000-0000-4000-8000-000000000002"
MONITOR = "00000000-0000-4000-8000-000000000001"
NORMAL = "00000000-0000-4000-8000-000000000003"
ANSWERS = ('{"sum":13,"product":42}', "READY", '{"status":"sources_unavailable","citations":[]}')


@pytest.fixture
def program():
    source_revision = live.revision()
    with offline_environment() as isolation:
        import ai4ia_api
        from ai4ia_api.catalog import load_catalog
        from ai4ia_api.gateway.client import ModelGatewayClient
        from ai4ia_api.main import create_app
        from app.api.tests.conftest import make_settings
        from app.api.tests.test_auth_entra import BARE_GUID, ISSUER, KID, TENANT, _new_keypair, _provider
        from fastapi.testclient import TestClient

        assert ROOT / "app" / "api" / "src" in Path(ai4ia_api.__file__).parents
        _, prices = source_documents()
        model = next(entry for entry in load_catalog(None, "global", True).models if (
            entry.api == "chat" and entry.category in ("chat", "chat-fast")
            and entry.supportsSampling and not entry.reasoningEffortOptions and entry.id in prices["models"]
        ))
        policy = {
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
            group_policy_enabled=True, group_policy_json=json.dumps(policy),
            session_deletion_enabled=True, model_gateway_url="https://gateway.invalid",
            applicationinsights_connection_string=None,
        )
        private, jwks = _new_keypair(KID)
        calls = []
        responses = {}
        dataset = load_live_dataset()
        for case, answer in zip(dataset.cases, ANSWERS):
            responses[authored_prompt(case)] = answer

        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            assert request.url.host == "gateway.invalid"
            assert request.url.path.startswith("/deployments/")
            assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
            answer = responses[body["messages"][0]["content"]]
            return httpx.Response(200, json={
                "model": model.id,
                "choices": [{"message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
            })

        def token(subject=EVALUATOR, *, expired=False, roles=None):
            now = int(time.time())
            return jwt.encode({
                "aud": BARE_GUID, "iss": ISSUER, "tid": TENANT, "oid": subject, "appid": CLIENT,
                "iat": now - 60, "exp": now - 1 if expired else now + 3600,
                "roles": roles or [],
            }, private, algorithm="RS256", headers={"kid": KID})

        config = LiveConfig(
            enabled="true", api_origin="https://api.synthetic.invalid", api_audience=BARE_GUID,
            tenant_id=TENANT, client_id=CLIENT, actor_object_id=EVALUATOR,
            deployment_client_id=DEPLOY, model_id=model.id, limits_ack="finite-requests-not-a-bill-cap",
        )
        bind_token(config, token())
        identity = live.build_identity(dataset, source_revision, config)
        app = create_app(settings)
        with TestClient(app) as client:
            app.state.auth_provider = _provider(jwks, audience=BARE_GUID)
            http = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
            app.state.gateway = ModelGatewayClient(settings, http_client=http)
            assert app.state.evaluation_dispatch_guard.__module__ == "ai4ia_api.request_constraints"
            yield SimpleNamespace(
                client=client, config=config, dataset=dataset, identity=identity, token=token,
                calls=calls, isolation=isolation, settings=settings,
            )
            assert client.portal is not None
            client.portal.call(http.aclose)


class AppTransport:
    def __init__(self, program, *, subject=EVALUATOR, expired=False, mutate=None, roles=None):
        self.program = program
        self.headers = {"Authorization": f"Bearer {program.token(subject, expired=expired, roles=roles)}"}
        self.mutate = mutate
        self.requests = []

    def __call__(self, method, path, body, *, timeout, cleanup):
        payload = json.loads(body) if body is not None else None
        self.requests.append((method, path, payload))
        if self.mutate is not None:
            payload = self.mutate(method, path, payload)
        response = self.program.client.request(
            method, path, json=payload, headers=self.headers,
        )
        return HttpResult(response.status_code, response.content)


def run_program(program, transport=None, *, operation="run"):
    transport = transport or AppTransport(program)
    report = live.evaluate_live(
        program.dataset, program.identity, program.config, ApiClient(transport, Budget()),
        operation=operation,
    )
    return report, transport


def test_signed_actor_real_factory_driver_and_exact_cleanup(program):
    report, transport = run_program(program)
    assert report.gate == "passed", report.model_dump_json()
    assert report.coverage.passed == 3 and report.coverage.pass_denominator == 3
    assert report.lifecycle.status == "passed"
    assert report.identity.execution_capabilities_version == 1
    assert len(program.calls) == 3 and program.isolation.attempts == 0
    assert [body["messages"] for body in program.calls] == [
        [{"role": "user", "content": authored_prompt(case)}] for case in program.dataset.cases
    ]
    assert all(body.get("max_tokens") == 256 and "tools" not in body for body in program.calls)
    assert all(row.checks[-1].status == "passed" and row.measurements.cost_micro_usd is not None for row in report.cases)
    assert len([item for item in transport.requests if item[:2] == ("POST", "/api/sessions")]) == 4
    assert all("systemPrompt" not in (body or {}) for method, path, body in transport.requests if path == "/api/sessions")
    assert EVALUATOR not in report.model_dump_json()


def test_authenticated_preflight_has_no_fixture_or_provider_side_effect(program):
    report, transport = run_program(program, operation="preflight")
    assert report.api_preflight == "passed" and report.gate == "unknown"
    assert report.coverage.unknown == 3 and report.lifecycle.status == "not_run"
    assert len(transport.requests) == 3 and program.calls == []
    assert not any(path == "/api/sessions" for _, path, _ in transport.requests)


@pytest.mark.parametrize("subject,expired,roles", [
    (MONITOR, False, []), (NORMAL, False, []), (EVALUATOR, True, []),
    (EVALUATOR, False, ["admin"]),
])
def test_incompatible_actor_or_expired_signed_identity_never_creates_fixture(program, subject, expired, roles):
    passing, _ = run_program(program, operation="preflight")
    assert passing.api_preflight == "passed"
    report, transport = run_program(program, AppTransport(program, subject=subject, expired=expired, roles=roles))
    assert report.gate == "unknown" and report.coverage.unknown == 3
    assert program.calls == []
    assert not any(path == "/api/sessions" for _, path, _ in transport.requests)


@pytest.mark.parametrize("failure", ["missing_guard", "malformed_policy"])
def test_missing_actual_factory_or_changed_invalid_policy_refuses(program, failure):
    assert run_program(program, operation="preflight")[0].api_preflight == "passed"
    if failure == "missing_guard":
        program.client.app.state.evaluation_dispatch_guard = None
    else:
        program.settings.group_policy_json = "{invalid"
    report, transport = run_program(program)
    assert report.gate == "unknown" and report.coverage.unknown == 3
    assert program.calls == []
    assert not any(path == "/api/sessions" for _, path, _ in transport.requests)


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("when", ["before_preflight", "before_dispatch"])
def test_paused_evaluation_policy_preserves_denial_cleanup_and_coverage(program, paused, when):
    assert run_program(program, operation="preflight")[0].api_preflight == "passed"
    if when == "before_preflight":
        program.settings.group_policy_enabled = not paused

    dispatch_attempts = []

    def mutate(method, path, body):
        if method == "POST" and path == "/api/chat" and "sessionId" in body:
            dispatch_attempts.append(body["sessionId"])
            if when == "before_dispatch":
                program.settings.group_policy_enabled = not paused
        return body

    report, transport = run_program(program, AppTransport(program, mutate=mutate))
    creates = [
        body for method, path, body in transport.requests
        if (method, path) == ("POST", "/api/sessions")
    ]
    assert report.coverage.pass_denominator == 3
    if not paused:
        assert report.gate == "passed" and report.coverage.passed == 3
        assert len(program.calls) == len(dispatch_attempts) == 3
        assert len(creates) == 4
    else:
        assert report.gate == "unknown" and report.coverage.unknown == 3
        assert program.calls == []
        assert all(row.measurements.cost_micro_usd is None for row in report.cases)
        if when == "before_preflight":
            assert report.api_preflight == "unknown" and report.lifecycle.status == "not_run"
            assert creates == dispatch_attempts == []
        else:
            assert report.api_preflight == "passed" and report.lifecycle.status == "passed"
            assert len(dispatch_attempts) == 1 and len(creates) == 2
            assert report.cases[0].checks[-1].status == "passed"
            assert all(row.checks[-1].reason == "not_run" for row in report.cases[1:])


@pytest.mark.parametrize("control,value", [
    ("allowTools", True), ("allowAutomaticMemory", True), ("requireFreshSession", False),
])
def test_reduction_tampering_is_denied_by_real_dispatch_not_just_preflight(program, control, value):
    assert run_program(program, operation="preflight")[0].api_preflight == "passed"

    def mutate(method, path, body):
        if method == "POST" and path == "/api/chat" and "sessionId" in body:
            return {**body, control: value}
        return body

    report, _ = run_program(program, AppTransport(program, mutate=mutate))
    assert report.gate == "unknown" and len(report.cases) == 3
    assert report.cases[0].checks[-1].status == "passed"
    assert program.calls == []


def test_real_one_shot_claim_cannot_be_replayed(program):
    headers = {"Authorization": f"Bearer {program.token()}"}
    created = program.client.post("/api/sessions", json={
        "model": program.config.model_id, "libraryDocumentIds": [],
    }, headers=headers)
    assert created.status_code == 201
    session_id = created.json()["id"]
    body = {
        "sessionId": session_id, "model": program.config.model_id,
        "content": authored_prompt(program.dataset.cases[0]), "stream": False,
        **live.REQUEST_CONTROLS, "params": {"max_tokens": 256},
    }
    assert program.client.post("/api/chat", json=body, headers=headers).status_code == 200
    assert program.client.post("/api/chat", json=body, headers=headers).status_code == 409
    assert len(program.calls) == 1
    client = ApiClient(AppTransport(program), Budget())
    client.created(session_id)
    assert live.cleanup(client, session_id)


def test_wrong_owner_never_reads_or_cleans_another_actors_fixture(program):
    foreign_headers = {"Authorization": f"Bearer {program.token(NORMAL)}"}
    foreign = program.client.post("/api/sessions", json={
        "model": program.config.model_id, "libraryDocumentIds": [],
    }, headers=foreign_headers)
    assert foreign.status_code == 201
    foreign_id = foreign.json()["id"]
    good = AppTransport(program)

    def transport(method, path, body, **kwargs):
        result = good(method, path, body, **kwargs)
        payload = json.loads(body) if body else {}
        if path == "/api/sessions" and payload.get("title") == "authored synthetic evaluation":
            value = result.json()
            value["id"] = foreign_id
            return HttpResult(result.status, json.dumps(value).encode())
        return result

    report, _ = run_program(program, transport)
    assert report.gate == "unknown" and program.calls == []
    assert program.client.get(f"/api/sessions/{foreign_id}", headers=foreign_headers).status_code == 200
    assert report.cases[0].checks[-1].status == "unknown"
