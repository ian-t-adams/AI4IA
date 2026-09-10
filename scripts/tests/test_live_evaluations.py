"""No live calls: exercise the complete driver with finite HTTP and process fakes."""
from __future__ import annotations

import base64
import io
import json
import socket
import subprocess
import threading
import time
import uuid

import pytest
from pydantic import ValidationError

from scripts.evaluations import live, live_http
from scripts.evaluations.contracts import canonical_bytes, coverage, overall
from scripts.evaluations.live_contracts import (
    CLEANUP_REQUEST_RESERVE, ENV_FIELDS, LIVE_CHECK_IDS, MAX_HTTP_REQUESTS,
    MAX_LIVE_REPORT_BYTES, MAX_OUTPUT_TOKENS, MAX_RESPONSE_BYTES, TOKEN_ENV,
    LiveConfig, LiveError, LiveReport, bind_token, load_live_dataset, source_documents,
)
from scripts.evaluations.live_http import ApiClient, Budget, HttpResult
from scripts.evaluations.offline import offline_environment

POISON = "PRIVATE-user-session-prompt-response-tool-secret"
_ACTOR = "11111111-1111-4111-8111-111111111111"
_CLIENT = "22222222-2222-4222-8222-222222222222"
_TENANT = "33333333-3333-4333-8333-333333333333"
_AUDIENCE = "44444444-4444-4444-8444-444444444444"
_DEPLOY = "55555555-5555-4555-8555-555555555555"
_ANSWERS = (
    '{"sum":13,"product":42}', "READY",
    '{"status":"sources_unavailable","citations":[]}',
)


@pytest.fixture(scope="module")
def model():
    with offline_environment():
        from ai4ia_api.catalog import load_catalog

        _, prices = source_documents()
        return next(entry for entry in load_catalog(None, "global", True).models if (
            entry.api == "chat" and entry.category in ("chat", "chat-fast")
            and entry.supportsSampling and entry.id in prices["models"]
        ))


@pytest.fixture(scope="module")
def config(model):
    return LiveConfig(
        enabled="true", api_origin="https://api.synthetic.invalid", api_audience=f"api://{_AUDIENCE}",
        tenant_id=_TENANT, client_id=_CLIENT, actor_object_id=_ACTOR,
        deployment_client_id=_DEPLOY, model_id=model.id, limits_ack="finite-requests-not-a-bill-cap",
    )


@pytest.fixture(scope="module")
def dataset():
    return load_live_dataset()


@pytest.fixture(scope="module")
def identity(dataset, config):
    return live.build_identity(dataset, live.revision(), config)


def _token(config, **changes):
    body = {
        "aud": config.api_audience, "tid": config.tenant_id, "oid": config.actor_object_id,
        "appid": config.client_id, "iss": f"https://sts.windows.net/{config.tenant_id}/",
        "exp": int(time.time()) + 3600, "nbf": int(time.time()) - 30,
    }
    body.update(changes)

    def encode(value):
        return base64.urlsafe_b64encode(canonical_bytes(value)).decode().rstrip("=")

    return f"{encode({'alg': 'RS256'})}.{encode(body)}.synthetic-signature"


def _proof(session, *, verified):
    return {
        "sessionId": session, "state": "cleanup_verified" if verified else "pending",
        "phase": "complete" if verified else "messages",
        "messagesVerified": verified, "documentsVerified": verified, "attachmentsVerified": verified,
        "requestedAt": "2026-09-10T12:00:00Z", "lastVerifiedAt": "2026-09-10T12:00:01Z" if verified else None,
        "pendingUploads": [], "pendingUploadsTruncated": False,
        "scope": "conversation_content_and_inline_originals",
        "backupsErased": False, "coordinationRetained": True, "autonomousCleanup": False,
    }


def _message(model_id, content):
    _, prices = source_documents()
    rate = prices["models"][model_id]
    return {
        "id": uuid.uuid4().hex, "role": "assistant", "status": "complete", "content": content,
        "executionReceipt": {
            "status": "complete", "partial": False, "truncated": False,
            "prompt": [{"role": "user", "content": {"text": POISON}}],
            "correlationId": POISON, "notes": [POISON],
            "contextBlocks": [], "toolsOffered": [], "toolsOfferedCount": 0,
            "toolCalls": [], "toolCallCount": 0, "delegations": [], "modelRequests": [],
            "approvalsRequested": 0, "approvalsGranted": 0,
            "runtime": {
                "modelId": model_id, "api": "chat", "agent": None,
                "instructionSha256": "a" * 64, "modelCallCount": 1,
                "modelCalls": [{
                    "modelId": model_id, "api": "chat", "httpAttempts": 1,
                    "coverage": "recorded", "providerCompleted": True,
                    "parameters": {"maxOutputTokens": MAX_OUTPUT_TOKENS, "outputTokenField": "max_tokens"},
                    "usageKnown": True, "usageComplete": True, "promptTokens": 20, "completionTokens": 10,
                    "cost": {
                        "coverage": "known", "estCostMicroUsd": 100,
                        "currency": "USD", "pricingBasis": "input_output_tokens",
                        "priceVersion": prices["version"], "priceInputPer1M": rate["inputPer1M"],
                        "priceOutputPer1M": rate["outputPer1M"],
                    },
                }],
            },
        },
    }


class SyntheticAPI:
    def __init__(self, model, dataset, *, fault=None):
        self.model, self.dataset, self.fault = model, dataset, fault
        self.requests = []
        self.sessions = {}
        self.chat_count = 0
        self.reconciles = []

    def __call__(self, method, path, body, *, timeout, cleanup):
        payload = json.loads(body) if body else None
        self.requests.append((method, path, payload, timeout, cleanup))
        assert 0 < timeout <= 45

        def result(status, value):
            return HttpResult(status, canonical_bytes(value))

        if path.startswith("/api/execution-capabilities?"):
            if self.fault in ("no_actor", "admin_actor"):
                return result(200, {"version": 1, "ready": False, "reason": "policy_unavailable"})
            value = {
                "version": 1, "ready": True, "ownerBound": True,
                "profile": "authored-synthetic-evaluation",
                "model": self.model.id, "api": "chat", "region": self.model.options[0].region,
                "reductionControlsVersion": 1,
                "constraints": {
                    "allowTools": False, "allowAutomaticMemory": False,
                    "requireFreshSession": True, "maxOutputTokens": 256,
                    "libraryDocumentIds": [],
                },
            }
            if self.fault == "monitor_actor":
                value["profile"] = "canary"
            if self.fault == "unbound_actor":
                value["ownerBound"] = False
            if self.fault == "weak_posture":
                value["constraints"]["allowTools"] = True
            return result(200, value)
        if path == "/api/models":
            if self.fault == "outage":
                raise LiveError("transport")
            catalog = self.model.model_dump(mode="json")
            if self.fault == "unsupported":
                catalog["api"] = "responses"
            return result(200, {"models": [catalog], "ignored-secret": POISON})
        if path == "/api/chat" and payload.get("content") is None:
            fields = ("content", "sessionId") if self.fault == "old_api" else (
                "allowTools", "allowAutomaticMemory", "requireFreshSession", "content", "sessionId",
            )
            return result(422, {"detail": [{"loc": ["body", field], "input": POISON} for field in fields]})
        if path == "/api/sessions":
            session = uuid.uuid4().hex
            self.sessions[session] = {"verified": False, "message": None}
            if self.fault == "ambiguous_create":
                raise LiveError("transport")
            return result(201, {
                "id": session, "model": self.model.id, "libraryDocumentIds": [], "agentName": None,
            })
        if path == "/api/chat":
            assert payload["stream"] is False and payload["allowTools"] is False
            assert payload["allowAutomaticMemory"] is False and payload["params"]["max_tokens"] == MAX_OUTPUT_TOKENS
            assert payload["requireFreshSession"] is True
            index = self.chat_count
            self.chat_count += 1
            message = _message(self.model.id, _ANSWERS[index])
            if self.fault == "wrong_answer" and index == 1:
                message["content"] = "OVERRIDE"
            if self.fault == "unknown_usage":
                message["executionReceipt"]["runtime"]["modelCalls"][0]["usageKnown"] = False
            if self.fault == "tool":
                message["executionReceipt"]["toolCalls"] = [{"tool": POISON}]
                message["executionReceipt"]["toolCallCount"] = 1
            self.sessions[payload["sessionId"]]["message"] = message
            return result(200, {"message": message})
        session = path.split("/")[3]
        assert session in self.sessions
        state = self.sessions[session]
        if path.endswith("/messages"):
            return result(200, [{"role": "user", "content": POISON}, state["message"]])
        if method == "DELETE":
            return HttpResult(204 if self.fault == "legacy" else 202, b"")
        if path.endswith("/deletion/reconcile"):
            self.reconciles.append(session)
            if self.fault != "cleanup_failure" or self.chat_count == 0:
                state["verified"] = True
            return result(200, _proof(session, verified=state["verified"]))
        if path.endswith("/deletion"):
            if self.fault == "legacy":
                return HttpResult(404, b"")
            return result(200, _proof(session, verified=state["verified"]))
        raise AssertionError("unexpected route")


def _run(model, dataset, config, identity, *, fault=None):
    api = SyntheticAPI(model, dataset, fault=fault)
    budget = Budget()
    report = live.evaluate_live(dataset, identity, config, ApiClient(api, budget))
    return report, api


def test_live_driver_full_denominators_oracles_cleanup_and_privacy(model, dataset, config, identity):
    report, api = _run(model, dataset, config, identity)
    assert report.gate == "passed" and report.complete
    assert report.coverage.total == report.coverage.passed == report.coverage.pass_denominator == 3
    assert report.http_attempts == len(api.requests) == 29
    assert report.lifecycle.status == "passed" and report.lifecycle.http_attempts == 5
    assert api.chat_count == 3 and len(api.sessions) == 4
    assert all(state["verified"] for state in api.sessions.values())
    assert len(api.reconciles) == 4
    assert all(row.measurements.cost_micro_usd == 100 for row in report.cases)
    for name in ("tool_choice", "tool_feedback", "citations", "approval", "safety", "workflow"):
        assert report.check_coverage[name].unscored == 3
    text = report.model_dump_json()
    for forbidden in (POISON, config.api_origin, config.client_id, config.actor_object_id, *api.sessions):
        assert forbidden not in text
    assert report.identity.provider_observed_model_version is None
    assert report.identity.deployed_application_revision is None
    assert report.bill_cap == "not_proven"


@pytest.mark.parametrize("fault", [
    "outage", "old_api", "unsupported", "legacy", "ambiguous_create",
    "no_actor", "admin_actor", "monitor_actor", "unbound_actor", "weak_posture",
])
def test_refused_preconditions_do_not_invoke_models_or_omit_cases(model, dataset, config, identity, fault):
    control, good = _run(model, dataset, config, identity)
    report, api = _run(model, dataset, config, identity, fault=fault)
    assert control.gate == "passed" and good.chat_count == 3
    assert report.gate == "unknown" and report.coverage.unknown == 3
    assert len(report.cases) == 3 and api.chat_count == 0
    assert all(len(row.checks) == len(LIVE_CHECK_IDS) for row in report.cases)
    if fault in ("outage", "old_api", "unsupported", "no_actor", "admin_actor", "monitor_actor", "unbound_actor", "weak_posture"):
        assert not api.sessions
    if fault == "ambiguous_create":
        assert len(api.sessions) == 1
        assert not any(method == "DELETE" for method, *_ in api.requests)


@pytest.mark.parametrize("fault,check", [
    ("wrong_answer", "content"), ("tool", "isolation"), ("unknown_usage", "cost"),
    ("cleanup_failure", "cleanup"),
])
def test_live_failures_are_retained_with_passing_controls(model, dataset, config, identity, fault, check):
    passing, _ = _run(model, dataset, config, identity)
    assert passing.gate == "passed"
    report, api = _run(model, dataset, config, identity, fault=fault)
    assert report.gate != "passed" and report.coverage.pass_denominator == 3
    assert any(item.id == check and item.status in ("failed", "unknown") for row in report.cases for item in row.checks)
    if fault == "wrong_answer":
        assert report.gate == "failed" and report.complete
        assert api.chat_count == 3
    else:
        assert not report.complete and api.chat_count == 1
    if fault == "unknown_usage":
        assert report.cases[0].measurements.cost_micro_usd is None


@pytest.mark.parametrize("field,value", [
    ("messagesVerified", False), ("documentsVerified", False), ("attachmentsVerified", False),
    ("pendingUploads", [{"id": POISON}]), ("pendingUploadsTruncated", True),
    ("lastVerifiedAt", None), ("lastVerifiedAt", "yesterday"), ("state", "pending"),
    ("backupsErased", True), ("autonomousCleanup", True), ("sessionId", POISON),
])
def test_cleanup_requires_actual_exact_owner_proof(field, value):
    session = uuid.uuid4().hex
    proof = _proof(session, verified=True)
    assert live._cleanup_proof(proof, session)
    proof[field] = value
    assert not live._cleanup_proof(proof, session)


def test_request_budget_includes_probes_reads_and_cleanup_reserve():
    calls = []

    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        return HttpResult(200, b"{}")

    budget = Budget(http_attempts=MAX_HTTP_REQUESTS - CLEANUP_REQUEST_RESERVE - 1)
    api = ApiClient(transport, budget)
    api.request("GET", "/api/models")
    with pytest.raises(LiveError, match="bounds"):
        api.request("GET", "/api/models")
    session = api.created(uuid.uuid4().hex)
    for _ in range(CLEANUP_REQUEST_RESERVE):
        api.request("GET", f"/api/sessions/{session}/deletion", cleanup=True)
    with pytest.raises(LiveError, match="bounds"):
        api.request("GET", f"/api/sessions/{session}/deletion", cleanup=True)
    assert len(calls) == 1 + CLEANUP_REQUEST_RESERVE


def test_no_existing_session_or_unapproved_route_can_be_accessed():
    calls = []
    api = ApiClient(lambda *a, **k: calls.append(a), Budget())
    session = uuid.uuid4().hex
    for method, path, body in (
        ("GET", "/api/sessions", None), ("DELETE", f"/api/sessions/{session}", None),
        ("POST", "/api/chat", {"sessionId": session, "content": POISON}),
        ("GET", "https://provider.invalid", None), ("GET", "/api/memories", None),
    ):
        with pytest.raises(LiveError, match="configuration"):
            api.request(method, path, body)
    assert calls == [] and api.budget.http_attempts == 0


@pytest.mark.parametrize("change", [
    {"oid": _DEPLOY}, {"tid": _DEPLOY}, {"appid": _DEPLOY}, {"aud": "https://management.azure.com/"},
    {"scp": "user_impersonation"}, {"exp": 1}, {"tid": {}}, {"iss": POISON},
])
def test_token_local_binding_refuses_wrong_actor_audience_or_delegation(config, change):
    bind_token(config, _token(config))
    with pytest.raises(LiveError, match="identity"):
        bind_token(config, _token(config, **change))


@pytest.mark.parametrize("change", [
    {"api_origin": "http://api.synthetic.invalid"}, {"api_origin": "https://user:pass@api.synthetic.invalid"},
    {"api_origin": "https://api.synthetic.invalid/?token=secret"}, {"api_origin": "https://127.0.0.1"},
    {"api_origin": "https://api.synthetic.invalid:444"}, {"deployment_client_id": _CLIENT},
    {"limits_ack": "approved"}, {"api_audience": "https://management.azure.com/"},
])
def test_configuration_cannot_reuse_deploy_actor_or_redirect_authority(config, change):
    assert LiveConfig.model_validate(config.model_dump())
    with pytest.raises(ValidationError):
        LiveConfig.model_validate({**config.model_dump(), **change})


@pytest.mark.parametrize("private", ["127.0.0.1", "10.0.0.1", "::1", "::ffff:10.0.0.1", "169.254.169.254"])
def test_dns_requires_every_answer_public_and_pins_one_address(monkeypatch, private):
    def answers(address):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answers("8.8.8.8"))
    assert live_http.public_address("api.synthetic.invalid") == "8.8.8.8"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answers("8.8.8.8") + answers(private))
    with pytest.raises(LiveError, match="transport"):
        live_http.public_address("api.synthetic.invalid")


def test_transport_bound_rejects_redirects_and_oversized_responses():
    api = ApiClient(lambda *a, **k: HttpResult(200, b"{}"), Budget())
    assert api.request("GET", "/api/models").status == 200
    api.transport = lambda *a, **k: HttpResult(302, b"")
    with pytest.raises(LiveError, match="http"):
        api.request("GET", "/api/models")
    api.transport = lambda *a, **k: HttpResult(200, b"x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(LiveError, match="bounds"):
        api.request("GET", "/api/models")


def test_worker_is_clean_bounded_and_invalid_output_marks_every_case_unknown(model, dataset, config, identity, monkeypatch):
    report, _ = _run(model, dataset, config, identity)
    payload = canonical_bytes(report.model_dump(mode="json"))
    processes = []

    class Process:
        def __init__(self, args, **kwargs):
            self.stdout = io.BytesIO(payload)
            self.args, self.options, self.killed = args, kwargs, False
            processes.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def wait(self, timeout):
            assert timeout <= MAX_RUN_SECONDS + 15
            return 0

        def kill(self):
            self.killed = True

    from scripts.evaluations.live_contracts import MAX_RUN_SECONDS

    monkeypatch.setenv("AZURE_CLIENT_SECRET", POISON)
    monkeypatch.setenv("HTTPS_PROXY", POISON)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", POISON)
    monkeypatch.setenv("PYTHONPATH", POISON)
    monkeypatch.setattr(subprocess, "Popen", Process)
    assert live.run_worker(dataset, identity, config, _token(config)).gate == "passed"
    assert processes[0].args[1:3] == ["-I", "-B"]
    env = processes[0].options["env"]
    assert all(name not in env for name in ("AZURE_CLIENT_SECRET", "HTTPS_PROXY", "APPLICATIONINSIGHTS_CONNECTION_STRING", "PYTHONPATH"))
    assert processes[0].options["stderr"] == subprocess.DEVNULL
    payload = b"x" * (MAX_LIVE_REPORT_BYTES + 2)
    unknown = live.run_worker(dataset, identity, config, _token(config))
    assert unknown.coverage.unknown == 3
    assert unknown.http_attempts is None and unknown.response_bytes is None
    assert all(row.measurements.cost_micro_usd is None for row in unknown.cases)
    assert processes[-1].killed
    assert processes[-1].stdout.tell() == MAX_LIVE_REPORT_BYTES + 1


def test_report_validation_and_exclusive_output(model, dataset, config, identity, tmp_path):
    report, _ = _run(model, dataset, config, identity)
    path = tmp_path / "report.json"
    live.write_live_report(report, path)
    assert POISON not in path.read_text()
    with pytest.raises(FileExistsError):
        live.write_live_report(report, path)
    raw = report.model_dump(mode="json")
    raw["cases"].pop()
    with pytest.raises(ValidationError):
        LiveReport.model_validate(raw)
    raw = report.model_dump(mode="json")
    raw["cases"][0]["checks"].pop()
    with pytest.raises(ValidationError):
        LiveReport.model_validate(raw)


@pytest.mark.parametrize("dimension,new_status", [
    ("execution", "unscored"), ("output_schema", "unscored"), ("approval", "passed"),
])
def test_reports_cannot_hide_unmeasured_quality_by_rewriting_coverage(
    model, dataset, config, identity, dimension, new_status,
):
    report, _ = _run(model, dataset, config, identity)
    raw = report.model_dump(mode="json")
    for check in raw["cases"][0]["checks"]:
        if check["id"] == dimension:
            check.update(status=new_status, reason="not_applicable" if new_status == "unscored" else "matched")
    for row in raw["cases"]:
        row["status"] = overall([check["status"] for check in row["checks"]])
    raw["coverage"] = coverage([row["status"] for row in raw["cases"]]).model_dump()
    raw["check_coverage"] = {
        name: coverage([row["checks"][index]["status"] for row in raw["cases"]]).model_dump()
        for index, name in enumerate(LIVE_CHECK_IDS)
    }
    with pytest.raises(ValidationError, match="incompatible_scoring_coverage"):
        LiveReport.model_validate(raw)


def test_task_admission_keeps_its_cleanup_calls_and_time_available():
    now = [0.0]
    budget = Budget(clock=lambda: now[0], http_attempts=MAX_HTTP_REQUESTS - CLEANUP_REQUEST_RESERVE - 3)
    budget.require_work(3)
    budget.http_attempts += 1
    with pytest.raises(LiveError, match="bounds"):
        budget.require_work(3)
    now[0] = 195.0
    with pytest.raises(LiveError, match="timeout"):
        budget.begin(cleanup=False)
    assert budget.begin(cleanup=True) == 45.0


def test_https_bounds_dns_and_header_wait_without_replaying(config, monkeypatch):
    release = threading.Event()
    finished = threading.Event()
    connections = []

    def resolve(_host):
        release.wait(timeout=2)
        finished.set()
        return "8.8.8.8"

    class Connection:
        def __init__(self, *_args):
            self.sock = None
            connections.append(self)

        def request(self, method, path, body, headers):
            assert headers["Authorization"] == "Bearer synthetic-token"
            assert headers["Accept-Encoding"] == "identity" and "Cookie" not in headers

        def getresponse(self):
            return SimpleResponse()

        def close(self):
            pass

    class SimpleResponse:
        status = 200

        def getheader(self, _name, default=None):
            return default

        def read1(self, _amount):
            return b""

    monkeypatch.setattr(live_http, "public_address", resolve)
    monkeypatch.setattr(live_http, "_PinnedHTTPSConnection", Connection)
    release.set()
    assert live_http.HTTPS(config, "synthetic-token", Budget())(
        "GET", "/api/models", None, timeout=1, cleanup=False,
    ).status == 200
    assert len(connections) == 1
    release.clear()
    finished.clear()
    try:
        with pytest.raises(LiveError, match="timeout"):
            live_http.HTTPS(config, "synthetic-token", Budget())(
                "GET", "/api/models", None, timeout=0.02, cleanup=False,
            )
        assert len(connections) == 1
    finally:
        release.set()
        assert finished.wait(timeout=1)


def test_https_keeps_tls_host_while_connecting_to_validated_ip(monkeypatch):
    seen = []

    class Socket:
        def close(self):
            pass

    class Context:
        def wrap_socket(self, sock, *, server_hostname):
            seen.append(("tls", server_hostname))
            return sock

    monkeypatch.setattr(live_http.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(live_http.socket, "create_connection", lambda address, **kwargs: (
        seen.append(("tcp", address)) or Socket()
    ))
    connection = live_http._PinnedHTTPSConnection("api.synthetic.invalid", "8.8.8.8", 1)
    connection.connect()
    connection.close()
    assert seen == [("tcp", ("8.8.8.8", 443)), ("tls", "api.synthetic.invalid")]


def test_disabled_cli_and_unapproved_inputs_never_start_worker(monkeypatch, capsys):
    monkeypatch.delenv(ENV_FIELDS["enabled"], raising=False)
    calls = []
    monkeypatch.setattr(live, "run_worker", lambda *a, **k: calls.append(a))
    assert live.main(["preflight"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["termination"] == "disabled" and report["coverage"]["unknown"] == 3
    for flags in (["--production-content"], ["--judge", "paid"]):
        assert live.main(["run", *flags]) == 2
        assert "disabled" in capsys.readouterr().err
    assert calls == []


def test_readonly_preflight_observes_policy_without_fixtures_or_quality(
    config, model, dataset, identity, monkeypatch, capsys,
):
    for key, name in ENV_FIELDS.items():
        monkeypatch.setenv(name, getattr(config, key))
    monkeypatch.setenv(TOKEN_ENV, _token(config))
    api = SyntheticAPI(model, dataset)

    def run_worker(data, source, configuration, _token, *, operation):
        assert operation == "preflight"
        return live.evaluate_live(data, source, configuration, ApiClient(api, Budget()), operation=operation)

    monkeypatch.setattr(live, "run_worker", run_worker)
    assert live.main(["preflight"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["api_preflight"] == "passed" and report["gate"] == "unknown"
    assert report["coverage"]["unknown"] == 3
    assert report["lifecycle"]["status"] == "not_run"
    assert len(api.requests) == 3 and not api.sessions and api.chat_count == 0
