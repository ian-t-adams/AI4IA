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
    ACK_HEADER, ATTEMPT_HEADER, ATTEMPT_VERSION, PROXY_PROOF_HEADER,
    VerifiedGatewayCapability, current_attempt_envelope, no_replay_scope,
)
from ai4ia_api.gateway.client import ModelGatewayClient, ModelGatewayError
from ai4ia_api.hard_quota.dispatch import admission_scope
from ai4ia_api.hard_quota.models import MAX_QUANTITY, QuotaError
from tests.test_hard_quota_dispatch import DEPLOYMENT, Harness, response_for


class FixtureVerifier:
    """Local deterministic compatibility evidence; never an app factory."""

    def __init__(self):
        self.capability = VerifiedGatewayCapability(
            gateway_url="https://gateway.test/openai", proxy_image="sha256:" + "1" * 64,
            apim_policy_sha256="2" * 64, topology_sha256="3" * 64, catalog_sha256="4" * 64,
            expires_at=time.time() + 240,
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
    ("anthropic", False), ("anthropic", True), ("embedding", False),
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
            await invoke(client, api, stream=stream)
        assert len(sent) == len(observed) == verifier.verified == 1
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
        with pytest.raises(QuotaError, match="binding"):
            current_attempt_envelope(
                "chat", json.loads(request.content), deployment=DEPLOYMENT,
                target=str(request.url), owner="alice",
            )
        return original("chat", request)

    response[0] = reply
    try:
        with admission_scope(harness.controller, "alice"), no_replay_scope("alice"):
            await invoke(client)
        assert len(sent) == 1
    finally:
        await client._http.aclose()
