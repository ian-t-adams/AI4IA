"""Actual adapted-client sends and pre-admission proof, with an offline verifier."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import nullcontext
from dataclasses import replace

import httpx
import pytest

from ai4ia_api.gateway import client as gateway_module
from ai4ia_api.gateway.attempts import (
    ACK_HEADER, ATTEMPT_HEADER, ATTEMPT_OPERATIONS, ATTEMPT_PATH, ATTEMPT_VERSION,
    PROXY_PROOF_HEADER, GatewayRouteBinding, VerifiedGatewayCapability,
    current_attempt_envelope, no_replay_scope,
)
from ai4ia_api.gateway.client import ModelGatewayClient, ModelGatewayError
from ai4ia_api.hard_quota.dispatch import admission_scope
from ai4ia_api.hard_quota.models import MAX_QUANTITY, QuotaError
from ai4ia_api.model_evidence import ModelCallRecorder
from tests.test_hard_quota_dispatch import DEPLOYMENT, Harness, response_for


class FixtureVerifier:
    """Local deterministic compatibility evidence; never an app factory."""

    def __init__(self):
        apim_id = (
            "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/fixture"
            "/providers/Microsoft.ApiManagement/service/fixture"
        )
        self.capability = VerifiedGatewayCapability(
            gateway_url="https://gateway.test/openai", proxy_image="sha256:" + "1" * 64,
            apim_policy_sha256="2" * 64, topology_sha256="3" * 64, catalog_sha256="4" * 64,
            expires_at=time.time() + 240,
            api_image="sha256:" + "5" * 64,
            route=GatewayRouteBinding(
                apim_url="https://apim.test", api_resource_id=apim_id + "/apis/ai4ia-attempts-v1",
                api_revision="1", subscription_resource_id=apim_id + "/subscriptions/fixture-proxy-attempts-v1",
                subscription_scope=apim_id + "/apis/ai4ia-attempts-v1",
                operations=ATTEMPT_OPERATIONS, evidence_epoch="6" * 64,
            ),
        )
        self.verified = 0

    async def verify(self, capability):
        assert capability == self.capability
        self.verified += 1


@pytest.fixture
def wire(monkeypatch):
    sent = []
    response = [None]

    def send(request):
        sent.append(request)
        result = response[0](request) if response[0] else response_for("chat", request)
        value = request.headers.get(ATTEMPT_HEADER)
        if value:
            assert hashlib.sha256(request.content).hexdigest() == value.rsplit(".", 1)[1]
            assert request.headers["s7p-key"] == "fixture-ingress"
            assert PROXY_PROOF_HEADER not in request.headers
            result.headers[ACK_HEADER] = value.rsplit(".", 1)[0]
        return result

    transport = httpx.MockTransport(send)
    monkeypatch.setattr(
        gateway_module, "bounded_http_client",
        lambda timeout: httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=True),
    )
    return sent, response, transport


def gateway(harness, transport, verifier):
    settings = harness.settings.model_copy(update={
        "model_gateway_auth_mode": "api_key", "model_gateway_api_key": "fixture-ingress",
        "model_gateway_api_key_header": "S7P-KEY",
        "gateway_attempts_v1_staged": True,
    })
    return ModelGatewayClient(
        settings, http_client=httpx.AsyncClient(transport=transport), attempt_verifier=verifier,
    )


async def invoke(client, api="chat", *, stream=False, params=None):
    if api == "embedding":
        return await client.embed(deployment=DEPLOYMENT, inputs=["hello"])
    kwargs = dict(deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}],
                  params=params, api=api)
    if stream:
        return [item async for item in client.stream(**kwargs)]
    return await client.complete(**kwargs)


@pytest.mark.parametrize("api,stream", [
    ("chat", False), ("chat", True), ("responses", False), ("responses", True),
    ("embedding", False),
])
async def test_real_adapter_exposes_bound_proof_before_admission(wire, api, stream, monkeypatch):
    sent, response, transport = wire
    response[0] = lambda request: response_for(api + ("-stream" if stream else ""), request)
    harness = Harness()
    verifier = FixtureVerifier()
    client = gateway(harness, transport, verifier)
    observed = []
    original = harness.controller.claim

    async def claim(context, surface, payload, deployment, target):
        assert not sent
        envelope = current_attempt_envelope(
            surface, payload, deployment=deployment, target=target, owner=context.owner,
        )
        assert envelope is not None and envelope.max_attempts == 1
        assert envelope.version == ATTEMPT_VERSION
        with pytest.raises(QuotaError, match="binding"):
            current_attempt_envelope(
                surface, {**payload, "changed": True}, deployment=deployment, target=target,
                owner=context.owner,
            )
        with pytest.raises(QuotaError, match="binding"):
            current_attempt_envelope(
                surface, payload, deployment=deployment, target=target, owner="bob",
            )
        observed.append(payload)
        return await original(context, surface, payload, deployment, target)

    monkeypatch.setattr(harness.controller, "claim", claim)
    try:
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            await invoke(client, api, stream=stream, params={"max_tokens": 20} if api == "responses" else None)
        assert len(sent) == len(observed) == verifier.verified == 1
        assert sent[0].url.path.startswith(ATTEMPT_PATH + "/")
        assert json.loads(sent[0].content) == observed[0]
        assert current_attempt_envelope("chat", {}, deployment=None, target=None, owner="alice") is None
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("defect", ["missing", "expired", "version", "hash", "target", "replaced", "auth"])
async def test_invalid_capability_refuses_before_paid_send_with_legacy_control(wire, defect):
    sent, _, transport = wire
    harness = Harness()
    verifier = FixtureVerifier()
    if defect == "missing":
        verifier.capability = None
    elif defect == "expired":
        verifier.capability = replace(verifier.capability, expires_at=time.time() - 1)
    elif defect == "version":
        verifier.capability = replace(verifier.capability, version="unknown")
    elif defect == "hash":
        verifier.capability = replace(verifier.capability, apim_policy_sha256="claimed")
    elif defect == "target":
        verifier.capability = replace(verifier.capability, gateway_url="https://elsewhere.test/openai")
    elif defect == "replaced":
        async def revoke(capability):
            verifier.capability = None
        verifier.verify = revoke
    client = gateway(harness, transport, verifier)
    if defect == "auth":
        client._api_key = None
    try:
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            with pytest.raises(QuotaError):
                await invoke(client)
        assert sent == []
        with admission_scope(harness.controller, "alice"):
            await invoke(client)
        assert len(sent) == 1 and ATTEMPT_HEADER not in sent[0].headers
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("strict", [True, False])
async def test_stream_parameter_fallback_is_a_real_second_send_only_in_legacy(wire, strict):
    sent, response, transport = wire
    response[0] = lambda request: (
        httpx.Response(400, json={"error": "stream_options"})
        if len(sent) == 1 else response_for("chat-stream", request)
    )
    harness = Harness()
    client = gateway(harness, transport, FixtureVerifier())
    client._stream_include_usage = True
    try:
        with admission_scope(harness.controller, "alice"), (no_replay_scope("alice") if strict else nullcontext()):
            if strict:
                with pytest.raises(ModelGatewayError) as error:
                    await invoke(client, stream=True)
                assert error.value.status_code == 400
            else:
                await invoke(client, stream=True)
        assert len(sent) == (1 if strict else 2)
        assert "stream_options" in json.loads(sent[0].content)
        if not strict:
            assert "stream_options" not in json.loads(sent[1].content)
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("params", [
    {"background": False}, {"tools": [{"type": "web_search"}]}, {"n": 2},
    {"modalities": ["audio"]}, {"container": "auto"}, {"unexpected": "value"},
])
async def test_unsupported_payload_is_not_silently_downgraded(wire, params):
    sent, _, transport = wire
    harness = Harness()
    client = gateway(harness, transport, FixtureVerifier())
    try:
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            with pytest.raises(QuotaError, match="Unsupported"):
                await invoke(client, params=params)
        assert not sent
        with admission_scope(harness.controller, "alice"):
            await invoke(client, params=params)
        assert len(sent) == 1
    finally:
        await client._http.aclose()


async def test_disabled_owner_still_denied_and_valid_owner_reserves_real_one_attempt_bound(wire):
    sent, _, transport = wire
    harness = Harness()
    client = gateway(harness, transport, FixtureVerifier())
    try:
        await harness.limits(disabled=True, tokensPerDay=120)
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            with pytest.raises(QuotaError, match="disabled"):
                await invoke(client)
        assert not sent
        await harness.limits(disabled=False, tokensPerDay=120)
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            await invoke(client)
        snapshot = await harness.store.read("alice")
        entry = next(iter(snapshot.state.entries.values()))
        assert entry.bounds.attemptVersion == ATTEMPT_VERSION
        assert entry.bounds.maxAttempts == 1
        assert entry.bounds.amounts.tokens == 120
        assert len(sent) == 1
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("failure", ["cancel", "timeout", "lost-response", "redirect"])
async def test_unknown_outcome_holds_bound_and_never_replays(wire, failure):
    sent, response, transport = wire
    harness = Harness()
    client = gateway(harness, transport, FixtureVerifier())

    def fail(request):
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture", request=request)
        if failure == "lost-response":
            raise httpx.RemoteProtocolError("fixture", request=request)
        return httpx.Response(307, headers={"location": str(request.url)})

    response[0] = fail
    try:
        await harness.limits(tokensPerDay=120)
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            with pytest.raises((asyncio.CancelledError, ModelGatewayError)):
                await invoke(client)
            with pytest.raises(QuotaError):
                await invoke(client)
        assert len(sent) == 1
        snapshot = await harness.store.read("alice")
        assert next(iter(snapshot.state.entries.values())).phase == "unknown"
    finally:
        await client._http.aclose()


async def test_oversized_provider_usage_keeps_terminal_accounting_unknown(wire):
    sent, response, transport = wire
    harness = Harness()
    client = gateway(harness, transport, FixtureVerifier())
    response[0] = lambda request: httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": "done"}}],
        "usage": {"prompt_tokens": MAX_QUANTITY, "completion_tokens": 1, "total_tokens": MAX_QUANTITY + 1},
    })
    try:
        await harness.limits(tokensPerDay=120)
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            await invoke(client)
        record = next(iter((await harness.store.read("alice")).state.entries.values()))
        assert record.phase == "unknown" and record.charged.tokens == 120
        assert len(sent) == 1
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("defect", ["model", "path", "surface", "auth-flow", "duplicate-query"])
async def test_prepared_model_and_transport_are_the_actual_priced_operation(wire, defect):
    sent, _, transport = wire
    harness = Harness()
    client = gateway(harness, transport, FixtureVerifier())
    request = client.build_request(deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}])
    params = {"headers": request.headers, "json": request.json}
    surface = "chat"
    if defect == "model":
        request.json["model"] = "different-model"
    elif defect == "path":
        request.url = request.url.replace(DEPLOYMENT, "different-deployment")
    elif defect == "surface":
        surface = "embedding"
    elif defect == "duplicate-query":
        request.url += "&api-version=different"
    elif defect == "auth-flow":
        params["auth"] = httpx.BasicAuth("fixture", "fixture")
    try:
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            with pytest.raises(QuotaError):
                await client._post(
                    client._http, request.url, surface=surface, deployment=DEPLOYMENT,
                    payload=request.json, **params,
                )
        assert not sent
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            await invoke(client)
        assert len(sent) == 1
    finally:
        await client._http.aclose()


async def test_proof_is_consumed_and_cannot_authorize_another_dispatch(wire):
    sent, response, transport = wire
    harness = Harness()
    client = gateway(harness, transport, FixtureVerifier())
    original = response_for

    def reply(request):
        assert current_attempt_envelope(
            "chat", json.loads(request.content), deployment=DEPLOYMENT,
            target=str(request.url), owner="alice",
        ) is None
        return original("chat", request)

    response[0] = reply
    try:
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            await invoke(client)
        assert len(sent) == 1
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("bounded", [False, True])
async def test_responses_preserves_explicit_bounded_maximum_and_legacy_floor(wire, stream, bounded):
    sent, response, transport = wire
    harness = Harness()
    client = gateway(harness, transport, FixtureVerifier())
    response[0] = lambda request: response_for("responses-stream" if stream else "responses", request)
    recorder = ModelCallRecorder(model_id="fixture-text", deployment=DEPLOYMENT, pricing=harness.pricing)
    try:
        with recorder.bind(), admission_scope(harness.controller, "alice"), (
            no_replay_scope("alice") if bounded else nullcontext()
        ):
            await invoke(client, "responses", stream=stream, params={"max_tokens": 20})
        assert len(sent) == 1
        assert json.loads(sent[0].content)["max_output_tokens"] == (20 if bounded else 16384)
        evidence = recorder.snapshot()[0]
        assert evidence.parameters.maxOutputTokens == (20 if bounded else 16384)
        assert evidence.httpAttempts == 1
        if bounded:
            record = next(iter((await harness.store.read("alice")).state.entries.values()))
            assert record.bounds.amounts.tokens == 120
            assert record.bounds.amounts.microUsd == 140
            assert record.bounds.maxAttempts == 1
            assert evidence.admissions[0].reserved.tokens == 120
            assert evidence.admissions[0].reserved.microUsd == 140
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("maximum", [True, 0, -1, "20", 20.5, None, 21])
async def test_bounded_responses_invalid_or_over_model_maximum_fails_before_egress(wire, maximum):
    sent, response, transport = wire
    harness = Harness()
    client = gateway(harness, transport, FixtureVerifier())
    response[0] = lambda request: response_for("responses", request)
    try:
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            with pytest.raises(QuotaError):
                await invoke(client, "responses", params={"max_tokens": maximum})
        assert not sent
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            await invoke(client, "responses", params={"max_tokens": 20})
        assert len(sent) == 1
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("api,bounded", [
    ("chat", False), ("chat", True), ("responses", False), ("responses", True),
    ("anthropic", False),
])
async def test_real_stream_can_close_in_another_task_without_context_leak(wire, api, bounded):
    sent, response, transport = wire
    harness = Harness()
    client = gateway(harness, transport, FixtureVerifier())
    response[0] = lambda request: response_for(api + "-stream", request)
    try:
        with admission_scope(harness.controller, "alice"), (
            no_replay_scope("alice") if bounded else nullcontext()
        ):
            stream = client.stream(
                deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}],
                params={"max_tokens": 20}, api=api,
            )
            await anext(stream)
            await asyncio.create_task(stream.aclose())
            assert current_attempt_envelope("chat", {}, deployment=None, target=None, owner="alice") is None
        assert len(sent) == 1
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("catalog_api", ["chat", "anthropic"])
async def test_claude_is_not_a_versioned_route_even_on_a_shared_proxy_path(wire, stream, catalog_api):
    sent, response, transport = wire
    harness = Harness()
    harness.catalog.models[0].api = catalog_api
    verifier = FixtureVerifier()
    client = gateway(harness, transport, verifier)
    response[0] = lambda request: response_for("anthropic-stream" if stream else "anthropic", request)
    try:
        assert client.attempt_capability is not None
        assert client.attempt_capability_for("anthropic") is None
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            with pytest.raises(QuotaError, match="Unsupported versioned gateway"):
                await invoke(client, "anthropic", stream=stream)
        assert not sent and verifier.verified == 0
        with admission_scope(harness.controller, "alice"):
            await invoke(client, "anthropic", stream=stream)
        assert len(sent) == 1 and sent[0].url.path.startswith("/openai/")
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("defect", [
    "api", "scope", "subscription", "revision", "epoch", "operations", "apim-url", "api-image",
])
async def test_typed_route_readback_must_describe_the_isolated_api_and_key(wire, defect):
    sent, _, transport = wire
    harness, verifier = Harness(), FixtureVerifier()
    route = verifier.capability.route
    changes = {
        "api": {"api_resource_id": route.api_resource_id.replace("ai4ia-attempts-v1", "openai")},
        "scope": {"subscription_scope": route.subscription_scope.replace("ai4ia-attempts-v1", "openai")},
        "subscription": {"subscription_resource_id": route.subscription_resource_id.replace("proxy-attempts-v1", "proxy-models")},
        "revision": {"api_revision": ""},
        "epoch": {"evidence_epoch": "operator-acknowledged"},
        "operations": {"operations": (*ATTEMPT_OPERATIONS, ("POST", "/{*path}"))},
        "apim-url": {"apim_url": "https://apim.test/openai"},
    }
    verifier.capability = (
        replace(verifier.capability, api_image="latest") if defect == "api-image"
        else replace(verifier.capability, route=replace(route, **changes[defect]))
    )
    client = gateway(harness, transport, verifier)
    try:
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            with pytest.raises(QuotaError):
                await invoke(client)
        assert not sent and verifier.verified == 0
        with admission_scope(harness.controller, "alice"):
            await invoke(client)
        assert len(sent) == 1
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("staged,verified", [(False, False), (False, True), (True, False)])
async def test_staging_cannot_select_runtime_bounded_calls(wire, staged, verified):
    sent, _, transport = wire
    harness = Harness()
    client = gateway(harness, transport, FixtureVerifier() if verified else None)
    client._attempt_staged = staged
    try:
        assert client.attempt_capability is None
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            with pytest.raises(QuotaError):
                await invoke(client)
        assert not sent
        with admission_scope(harness.controller, "alice"):
            await invoke(client)
        assert len(sent) == 1 and ATTEMPT_HEADER not in sent[0].headers
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("path", [
    "/openai/%72esponses", "/openai/deployments/{deployment}/chat%2fcompletions",
    "/openai//deployments/{deployment}/chat/completions",
    "/openai/deployments/{deployment}/chat/completions/",
    "/openai/deployments/{deployment}/chat/completions?api-version=a&api-version=b",
    "/openai/deployments/{deployment}/chat/completions?api-version=a%26x",
    "/openai/deployments/{deployment}/chat/completions?subscription-key=wrong",
    "/openai/deployments/{deployment}/embeddings",
])
async def test_encoded_or_ambiguous_path_never_falls_back_to_ordinary(wire, path):
    sent, _, transport = wire
    harness, verifier = Harness(), FixtureVerifier()
    client = gateway(harness, transport, verifier)
    req = client.build_request(deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}])
    try:
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            with pytest.raises(QuotaError):
                await client._post(
                    client._http, "https://gateway.test" + path.format(deployment=DEPLOYMENT),
                    surface="chat", deployment=DEPLOYMENT, payload=req.json,
                    headers=req.headers, json=req.json,
                )
        assert not sent
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            await invoke(client)
        assert len(sent) == 1 and sent[0].url.path.startswith(ATTEMPT_PATH + "/")
    finally:
        await client._http.aclose()


@pytest.mark.parametrize("setting,value", [
    ("model_gateway_url", "http://gateway.test/openai"),
    ("model_gateway_url", "https://gateway.test/ai4ia-attempts-v1/openai"),
    ("model_gateway_api_key_header", "Ocp-Apim-Subscription-Key"),
    ("model_gateway_auth_mode", "none"),
    ("model_gateway_api_key", ""),
    ("gateway_chat_path", "/custom"),
    ("gateway_provider_style", "openai"),
])
def test_staging_startup_enforces_governed_ingress_without_constructing_capability(setting, value):
    from ai4ia_api.config import GatewayAuthMode, Settings

    settings = Harness().settings.model_copy(update={
        "hard_quota_enabled": False, "gateway_attempts_v1_staged": True,
        "model_gateway_auth_mode": GatewayAuthMode.api_key, "model_gateway_api_key": "fixture-ingress",
        "model_gateway_api_key_header": "S7P-KEY",
    })
    settings = Settings.model_validate(settings.model_dump())
    settings.validate_gateway_attempts_v1()
    invalid = settings.model_copy(update={setting: value})
    with pytest.raises(RuntimeError, match="STAGED"):
        invalid.validate_gateway_attempts_v1()
    invalid.gateway_attempts_v1_staged = False
    invalid.validate_gateway_attempts_v1()
    assert Settings(_env_file=None).gateway_attempts_v1_staged is False


async def test_catalog_provider_cannot_be_disguised_by_a_chat_route(wire):
    sent, _, transport = wire
    harness, verifier = Harness(), FixtureVerifier()
    harness.catalog.models[0].api = "anthropic"
    client = gateway(harness, transport, verifier)
    try:
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            with pytest.raises(QuotaError, match="Unsupported versioned gateway provider"):
                await invoke(client, "chat")
        assert not sent and verifier.verified == 0
        harness.catalog.models[0].api = "chat"
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            await invoke(client, "chat")
        assert len(sent) == 1
    finally:
        await client._http.aclose()
