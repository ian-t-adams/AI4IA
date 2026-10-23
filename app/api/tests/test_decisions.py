"""Synthetic native Decision responses through the actual API and HTTP transport."""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.decisions import DecisionExecution, DecisionRequest
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app
from ai4ia_api.usage.models import cost_bearing_attempt
from tests.conftest import FakeUsage, make_settings

MODEL = "Microsoft-Decision-1"
HEADERS = {"X-Dev-User": "decision-fixture"}
BODY = {
    "model": MODEL, "input": '{"message":"The demo button looks wrong.","repeat":true}', "inputFormat": "json",
    "questions": [
        {"name": "repeat", "type": "noul", "instructions": "Is this a repeated report?"},
        {"name": "team", "type": "choice", "instructions": "Select the team.",
         "criteria": {"design": "Visual appearance", "engineering": "Crashes"}},
        {"name": "severity", "type": "score", "instructions": "Rate severity.",
         "criteria": ["Cosmetic", "Minor", "Major"]},
    ],
}
NATIVE = {
    "model": "microsoft-decision-1",
    "answers": {
        "repeat": {"type": "noul", "noul": .9},
        "team": {"type": "choice", "choice": "design", "confidence": .8,
                 "probabilities": {"design": .8, "engineering": .2}},
        "severity": {"type": "score", "score": .3, "confidence": .9,
                     "probabilities": {"0": .8, "1": .1, "2": .1}},
    },
    "usage": {"input_tokens": 123, "output_tokens": 7},
}


class NativeTransport:
    def __init__(self):
        self.body = copy.deepcopy(NATIVE)
        self.status = 200
        self.calls = []
        self.raw = None
        self.headers = {}

    def handle(self, request):
        self.calls.append(request)
        return httpx.Response(
            self.status, headers={"Content-Type": "application/json", **self.headers},
            stream=httpx.ByteStream(self.raw if self.raw is not None else json.dumps(self.body).encode()),
        )


@pytest.fixture
def rig():
    settings = make_settings(admin_subjects="alice")
    app = create_app(settings)
    native = NativeTransport()
    with TestClient(app) as client:
        app.state.gateway = ModelGatewayClient(
            settings, httpx.AsyncClient(transport=httpx.MockTransport(native.handle)),
        )
        meter = FakeUsage()
        app.state.usage = meter
        yield client, native, meter


def test_native_round_trip_and_unknown_cost(rig):
    client, native, meter = rig
    response = client.post("/api/decisions/run", json=BODY, headers=HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["answers"] == NATIVE["answers"]
    assert result["reportedModel"] == NATIVE["model"]
    assert result["usage"] == {"inputTokens": 123, "outputTokens": 7, "totalTokens": 130, "known": True}
    assert result["latencyMs"] >= 0
    assert result["costKnown"] is False and result["estimatedCostUsd"] is None
    assert result["residency"] == "us"
    assert response.headers["cache-control"] == "no-store"
    assert len(native.calls) == 1
    sent = native.calls[0]
    assert sent.url.host == "gateway.test"
    assert sent.url.path == f"/deployments/{MODEL}-slurmfactory-eastus2-dz/systemone"
    assert not sent.url.query
    wire = json.loads(sent.content)
    assert wire == {
        "model": f"{MODEL}-slurmfactory-eastus2-dz",
        "state": json.loads(BODY["input"]),
        "questions": {q["name"]: {k: v for k, v in q.items() if k != "name"} for q in BODY["questions"]},
    }
    assert len(meter.calls) == 1
    row = meter.calls[0]
    assert row["usage"].total == 130 and row["provider_completed"]
    assert row["target"].provider == "microsoft_decision"
    assert "demo" not in repr(row)


@pytest.mark.parametrize("change", [
    {"input": " "}, {"input": "x" * 16001}, {"input": '{"a":1,"a":2}'},
    {"input": "[NaN]"}, {"input": "[1e999]"}, {"input": "null"},
    {"input": "[" * 18 + "1" + "]" * 18}, {"endpoint": "https://example.com"},
    {"questions": []}, {"questions": [BODY["questions"][0]] * 2},
    {"questions": [{"name": "q", "type": "chat", "instructions": "x"}]},
    {"questions": [{"name": "q", "type": "choice", "instructions": "x", "criteria": ["a", "b"]}]},
    {"questions": [{"name": "q", "type": "score", "instructions": "x", "criteria": ["a", "a"]}]},
    {"questions": [{"name": "q", "type": "noul", "instructions": "x", "criteria": {"maybe": "x"}}]},
])
def test_invalid_input_never_dispatches(rig, change):
    client, native, meter = rig
    result = client.post("/api/decisions/run", json={**BODY, **change}, headers=HEADERS)
    assert result.status_code == 422, result.text
    assert not native.calls and not meter.calls


def test_body_bound_and_duplicate_keys(rig):
    client, native, _ = rig
    assert client.post("/api/decisions/run", content=b"x" * 65537, headers=HEADERS).status_code == 413
    duplicate = json.dumps(BODY)[:-1] + ',"model":"other"}'
    assert client.post("/api/decisions/run", content=duplicate, headers=HEADERS).status_code == 422
    assert not native.calls


@pytest.mark.parametrize("mutation", [
    lambda b: b["answers"].pop("repeat"),
    lambda b: b["answers"]["repeat"].update(noul=True),
    lambda b: b["answers"]["repeat"].update(noul=1.01),
    lambda b: b["answers"]["team"].update(choice="missing"),
    lambda b: b["answers"]["team"].update(probabilities={"design": .2, "engineering": .2}),
    lambda b: b["answers"]["severity"].update(score=3),
    lambda b: b["answers"]["severity"].update(probabilities={"0": 1}),
    lambda b: b["answers"]["severity"].update(confidence="0.5"),
    lambda b: b.update(model=""),
    lambda b: b["usage"].update(input_tokens=True),
])
def test_malformed_native_answers_are_errors_but_metered(rig, mutation):
    client, native, meter = rig
    mutation(native.body)
    response = client.post("/api/decisions/run", json=BODY, headers=HEADERS)
    assert response.status_code == 502
    assert len(native.calls) == len(meter.calls) == 1
    assert meter.calls[0]["status"] == "error" and meter.calls[0]["provider_completed"]
    assert "answers" not in response.json()


@pytest.mark.parametrize("status", [302, 400, 401, 403, 429, 500, 503])
def test_upstream_errors_never_retry_or_expose_provider_body(rig, status):
    client, native, meter = rig
    native.status = status
    native.raw = b'{"secret":"private state should never appear"}'
    response = client.post("/api/decisions/run", json=BODY, headers=HEADERS)
    assert response.status_code == (429 if status == 429 else 502)
    assert len(native.calls) == len(meter.calls) == 1
    assert not meter.calls[0]["usage"].known
    assert "secret" not in response.text


@pytest.mark.parametrize("delay,expected", [("12", "12"), ("NaN", None), ("inf", None), ("999999", None), ("private header", None)])
def test_bounded_retry_guidance_is_visible_but_never_retried(rig, delay, expected):
    client, native, _ = rig
    native.status = 429
    native.headers["Retry-After"] = delay
    response = client.post("/api/decisions/run", json=BODY, headers=HEADERS)
    assert response.status_code == 429 and len(native.calls) == 1
    assert response.headers.get("retry-after") == expected
    if expected is not None:
        assert f"Wait {expected} seconds" in response.json()["detail"]
    else:
        assert "Run again later" in response.json()["detail"]


def test_missing_usage_stays_unknown_and_extra_provider_text_is_not_returned(rig):
    client, native, meter = rig
    native.body.pop("usage")
    native.body["explanation"] = "not part of the contract"
    response = client.post("/api/decisions/run", json=BODY, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["usage"]["totalTokens"] is None
    assert not meter.calls[0]["usage"].known
    assert "explanation" not in response.text


@pytest.mark.parametrize(
    "raw", [b"x" * 65537, b"{}", b'{"answers":NaN}', b'{"answers":{},"answers":{}}'],
    ids=["oversize", "missing", "nonfinite", "duplicate"],
)
def test_bounded_response_parser(rig, raw):
    client, native, meter = rig
    native.raw = raw
    assert client.post("/api/decisions/run", json=BODY, headers=HEADERS).status_code == 502
    assert len(native.calls) == len(meter.calls) == 1


def test_availability_residency_chat_exclusion_and_runtime_disable(rig):
    client, native, _ = rig
    catalog = client.app.state.catalog
    entry = catalog.get(MODEL)
    assert entry is not None and not entry.conversational
    assert entry not in catalog.conversational_models()
    assert client.get("/api/decisions/options", headers=HEADERS).json()["models"][0]["id"] == MODEL
    catalog.residencyPolicy = "eu"
    assert client.get("/api/decisions/options", headers=HEADERS).json() == {"models": []}
    assert client.post("/api/decisions/run", json=BODY, headers=HEADERS).status_code == 400
    catalog.residencyPolicy = "global"
    entry.runtimeEnabled = False
    assert client.get("/api/decisions/options", headers=HEADERS).json() == {"models": []}
    assert client.post("/api/decisions/run", json=BODY, headers=HEADERS).status_code == 400
    assert not native.calls
    entry.runtimeEnabled = True
    assert client.post("/api/decisions/run", json=BODY, headers=HEADERS).status_code == 200


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.asyncio
async def test_decision_is_never_dispatched_as_a_chat_protocol(api):
    native = NativeTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.handle)) as http:
        gateway = ModelGatewayClient(make_settings(), http)
        with pytest.raises(ValueError, match="cannot serve chat"):
            await gateway.complete(
                deployment=f"{MODEL}-slurmfactory-eastus2-dz",
                messages=[{"role": "user", "content": "synthetic"}], api=api,
            )
    assert not native.calls


def test_authentication_and_disabled_account(rig):
    client, native, _ = rig
    from ai4ia_api.auth.base import AuthError
    original = client.app.state.auth_provider

    class Reject:
        async def authenticate(self, credentials):
            raise AuthError("Authentication required.")

    client.app.state.auth_provider = Reject()
    assert client.post("/api/decisions/run", json=BODY, headers=HEADERS).status_code == 401
    assert client.get("/api/decisions/options", headers=HEADERS).status_code == 401
    client.app.state.auth_provider = original
    uid = client.get("/api/entitlement", headers=HEADERS).json()["userId"]
    assert client.put(
        f"/api/admin/entitlements/{uid}", json={"disabled": True}, headers={"X-Dev-User": "alice"},
    ).status_code == 200
    assert client.post("/api/decisions/run", json=BODY, headers=HEADERS).status_code == 403
    assert not native.calls


@pytest.mark.asyncio
async def test_cancellation_closes_native_transport_without_replay():
    started = asyncio.Event()
    closed = asyncio.Event()

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    calls = []
    async def send(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=SlowBody())

    settings = make_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        gateway = ModelGatewayClient(settings, http)
        task = asyncio.create_task(gateway.decide(
            deployment=f"{MODEL}-slurmfactory-eastus2-dz",
            request=DecisionRequest.model_validate(BODY), execution=DecisionExecution(),
        ))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("framing", ["length", "chunked", "truncated", "oversize"])
async def test_native_three_type_response_over_real_loopback_http(framing):
    from ai4ia_api.gateway.client import ModelGatewayError

    sends = []
    finished = asyncio.Event()
    native = json.dumps(NATIVE).encode()

    async def respond(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            sends.append(json.loads(await reader.readexactly(length)))
            body = b"x" * 65537 if framing == "oversize" else native
            if framing == "chunked":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
                for part in (body[:43], body[43:]):
                    writer.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                writer.write(b"0\r\n\r\n")
            else:
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + (body[:43] if framing == "truncated" else body)
                )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        gateway = ModelGatewayClient(make_settings(model_gateway_url=f"http://127.0.0.1:{port}"))
        execution = DecisionExecution()
        call = gateway.decide(
            deployment=f"{MODEL}-slurmfactory-eastus2-dz",
            request=DecisionRequest.model_validate(BODY), execution=execution,
        )
        if framing in {"truncated", "oversize"}:
            with pytest.raises(ModelGatewayError):
                await call
            assert not execution.usage.known
        else:
            response = await call
            assert response["answers"] == NATIVE["answers"]
            assert execution.usage.total == 130
        await asyncio.wait_for(finished.wait(), 5)
        assert len(sends) == 1
        assert sends[0]["state"] == json.loads(BODY["input"])


def test_attempt_failure_is_cost_unknown_in_ledger(rig):
    client, _, _ = rig
    from ai4ia_api.usage.service import UsageService
    from ai4ia_api.usage.pricing import load_pricing
    from ai4ia_api.usage.models import TokenUsage, UsageTarget
    service = UsageService(None, load_pricing())
    row = service.build_record(
        user_id="fixture", session_id="decision-playground", model_id=MODEL,
        target=UsageTarget.from_deployment(client.app.state.catalog.resolve_deployment(MODEL), provider="microsoft_decision"),
        usage=TokenUsage(known=False, complete=False, calls=1), status="error",
        agent=None, correlation_id=None, provider_completed=False,
    )
    assert not row.costKnown and row.estCostMicroUsd is None and cost_bearing_attempt(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [
    {"requestsPerMinute": 0}, {"tokensPerDay": 1000}, {"costPerDayMicroUsd": 1_000_000},
])
async def test_hard_admission_refuses_unknown_bounds_with_allowed_control(limits):
    from ai4ia_api.catalog import load_catalog
    from ai4ia_api.hard_quota.dispatch import admission_scope
    from ai4ia_api.hard_quota.models import QuotaError
    from tests.test_hard_quota_dispatch import Harness

    h = Harness()
    h.catalog.models.append(load_catalog().get(MODEL).model_copy(deep=True))
    native = NativeTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.handle)) as http:
        gateway = ModelGatewayClient(h.settings, http)
        await h.limits(**limits)
        with admission_scope(h.controller, "alice"), pytest.raises(QuotaError):
            await gateway.decide(
                deployment=f"{MODEL}-slurmfactory-eastus2-dz",
                request=DecisionRequest.model_validate(BODY), execution=DecisionExecution(),
            )
        assert not native.calls
        await h.limits()
        with admission_scope(h.controller, "alice"):
            await gateway.decide(
                deployment=f"{MODEL}-slurmfactory-eastus2-dz",
                request=DecisionRequest.model_validate(BODY), execution=DecisionExecution(),
            )
        assert len(native.calls) == 1


@pytest.mark.asyncio
async def test_group_model_policy_is_rechecked_and_does_not_grant_chat():
    from ai4ia_api.policy.context import bind_authenticated, clear_policy_context
    from tests.test_group_policy import service, user

    config = {"domains": {"models": {"default": {"allow": ["decision"]}}}}
    policy, _ = service(config)
    native = NativeTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.handle)) as http:
        gateway = ModelGatewayClient(policy.settings, http)
        try:
            for allowed in (False, True):
                config["domains"]["models"]["default"]["allow"] = ["decision"] if allowed else []
                policy.settings.group_policy_json = json.dumps(config)
                bind_authenticated(policy, user())
                operation = gateway.decide(
                    deployment=f"{MODEL}-slurmfactory-eastus2-dz",
                    request=DecisionRequest.model_validate(BODY), execution=DecisionExecution(),
                )
                if allowed:
                    await operation
                else:
                    from ai4ia_api.gateway.client import ModelGatewayError
                    with pytest.raises(ModelGatewayError):
                        await operation
                    assert not native.calls
            assert len(native.calls) == 1
            with pytest.raises(ValueError, match="unsupported model API"):
                await gateway.complete(
                    deployment=f"{MODEL}-slurmfactory-eastus2-dz",
                    messages=[{"role": "user", "content": "synthetic"}], api="decision",
                )
            assert len(native.calls) == 1
        finally:
            clear_policy_context()
