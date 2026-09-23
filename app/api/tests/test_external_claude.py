"""Actual Claude payloads, server gates and deployment-aware frozen prices."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ai4ia_api.catalog import load_catalog
from ai4ia_api.gateway.anthropic import anthropic_usage_to_chat
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.hard_quota.coverage import supported_attempt_payload
from ai4ia_api.main import create_app
from ai4ia_api.model_evidence import ModelCallRecorder
from ai4ia_api.routers.chat import _effective_params
from ai4ia_api.usage.models import TokenUsage
from ai4ia_api.usage.pricing import PriceRate, load_pricing
from tests.conftest import make_settings


def entries():
    return [m for m in load_catalog().models if m.deploymentTarget == "external-claude"]


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_supported_profile_reaches_native_payload_and_receipt(effort):
    gateway = ModelGatewayClient(make_settings(claude_enabled=True, claude_external_enabled=True))
    for model in entries():
        for option in model.options:
            body = gateway.build_anthropic_request(
                deployment=option.deploymentName, messages=[{"role": "user", "content": "synthetic"}],
                params={"reasoning_effort": effort, "temperature": 0.3, "top_p": 0.4},
            ).json
            assert body["output_config"] == {"effort": effort}
            assert body["thinking"] == {"type": "disabled"}
            assert not {"temperature", "top_p", "cache_control", "fallbacks"} & body.keys()
            recorder = ModelCallRecorder(model_id=model.id, deployment=option.deploymentName, pricing=load_pricing())
            call = recorder.start(option.deploymentName, "anthropic")
            call.request(body)
            call.report_usage(anthropic_usage_to_chat({"input_tokens": 10, "output_tokens": 4}), completed=True)
            snapshot = call.snapshot()
            assert snapshot.parameters.reasoningEffort == effort
            assert snapshot.cost.coverage == "known"
            assert not supported_attempt_payload("chat", body)
            assert gateway.attempt_capability_for("anthropic") is None


@pytest.mark.parametrize("effort", ["xhigh", "max", "none", "minimal", "HIGH", ""])
def test_unoffered_effort_refuses_in_both_http_selection_and_actual_adapter(effort):
    gateway = ModelGatewayClient(make_settings(claude_enabled=True, claude_external_enabled=True))
    for model in entries():
        with pytest.raises(HTTPException) as denied:
            _effective_params({"reasoning_effort": effort}, model)
        assert denied.value.status_code == 422
        with pytest.raises(ValueError, match="Unsupported effort"):
            gateway.build_anthropic_request(
                deployment=model.options[0].deploymentName,
                messages=[{"role": "user", "content": "synthetic"}], params={"reasoning_effort": effort},
            )
        assert gateway.build_anthropic_request(
            deployment=model.options[0].deploymentName,
            messages=[{"role": "user", "content": "synthetic"}], params={"reasoning_effort": "low"},
        ).json["output_config"] == {"effort": "low"}


@pytest.mark.parametrize("field", ["deploymentTarget", "anthropicThinking", "reasoningEffort", "samplingSupported", "toolCalling"])
def test_missing_required_catalog_profile_cannot_restore_adaptive_defaults(tmp_path, field):
    raw = json.loads((Path(__file__).parents[1] / "src" / "ai4ia_api" / "data" / "model_catalog.json").read_text())
    original = deepcopy(raw)
    for model in raw["models"]:
        if model.get("deploymentTarget") == "external-claude":
            model.pop(field)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_catalog(str(path))
    path.write_text(json.dumps(original))
    load_catalog.cache_clear()
    assert load_catalog(str(path)).get(entries()[0].id).anthropicThinking == "disabled"


async def test_gateway_gate_precedes_network_and_enabled_control_uses_proxy():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "synthetic"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        for enabled in (False, True):
            gateway = ModelGatewayClient(make_settings(
                claude_enabled=enabled, claude_external_enabled=enabled,
                model_gateway_url="https://proxy.test/openai",
            ), http_client=http)
            for model in entries():
                if enabled:
                    await gateway.complete(
                        deployment=model.options[0].deploymentName,
                        messages=[{"role": "user", "content": "synthetic"}], api="chat",
                    )
                else:
                    with pytest.raises(ValueError, match="disabled"):
                        await gateway.complete(
                            deployment=model.options[0].deploymentName,
                            messages=[{"role": "user", "content": "synthetic"}], api="chat",
                        )
                    assert not requests
    assert len(requests) == 2
    assert all(request.url.host == "proxy.test" for request in requests)
    assert all(json.loads(request.content)["thinking"] == {"type": "disabled"} for request in requests)


@pytest.mark.parametrize("stream", [False, True])
async def test_runtime_disabled_profile_refuses_before_gateway_dispatch(tmp_path, stream):
    model = entries()[0]
    deployment = model.options[0].deploymentName
    path = tmp_path / "catalog.json"
    requests = []

    def handler(request):
        requests.append(request)
        if stream:
            return httpx.Response(200, content=(
                'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n\n'
                'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"synthetic"}}\n\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
                'data: {"type":"message_stop"}\n\n'
            ), headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "synthetic"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        for enabled in (False, True):
            raw = model.model_dump()
            raw["runtimeEnabled"] = enabled
            path.write_text(json.dumps({"models": [raw]}), encoding="utf-8")
            load_catalog.cache_clear()
            catalog = load_catalog(str(path))
            assert catalog.for_deployment(deployment).anthropicThinking == "disabled"
            assert catalog.get(model.id) is catalog.for_deployment(deployment)
            assert catalog.get(model.id).runtimeEnabled is enabled
            assert (catalog.resolve_deployment(model.id) is not None) is enabled
            gateway = ModelGatewayClient(make_settings(
                claude_enabled=True, claude_external_enabled=True,
                model_catalog_path=str(path), model_gateway_url="https://proxy.test/openai",
            ), http_client=http)

            async def invoke():
                kwargs = {
                    "deployment": deployment, "api": "anthropic",
                    "messages": [{"role": "user", "content": "synthetic"}],
                }
                if stream:
                    return [chunk async for chunk in gateway.stream(**kwargs)]
                return await gateway.complete(**kwargs)

            if enabled:
                assert await invoke()
            else:
                with pytest.raises(ValueError, match="runtime-disabled"):
                    await invoke()
                assert requests == []
    assert len(requests) == 1
    assert requests[0].url.host == "proxy.test"
    assert json.loads(requests[0].content)["thinking"] == {"type": "disabled"}


def test_startup_and_public_catalog_do_not_accept_claude_flag_alone():
    with pytest.raises(RuntimeError, match="CLAUDE_EXTERNAL_ENABLED"):
        make_settings(claude_enabled=True).validate_runtime()
    for enabled in (False, True):
        with TestClient(create_app(make_settings(
            claude_enabled=enabled, claude_external_enabled=enabled,
        ))) as client:
            response = client.get("/api/models")
            assert response.status_code == 200
            offered = [m for m in response.json()["models"] if m["api"] == "anthropic"]
            assert len(offered) == (2 if enabled else 0)
            assert all(m["anthropicThinking"] == "disabled" for m in offered)
            assert all(m["reasoningEffortOptions"] == ["low", "medium", "high"] for m in offered)


@pytest.mark.parametrize("read", [0, 20])
def test_exact_sku_rates_include_cached_read_and_output_without_region_guessing(read):
    book = load_pricing()
    for model in entries():
        for option in model.options:
            expected = (5.0, 25.0, 0.5) if model.id == "claude-opus-5" else (2.0, 10.0, 0.2)
            if option.sku == "DataZoneStandard":
                expected = (5.5, 27.5, 0.55)
            price = book.estimate(
                model.id, deployment=option.deploymentName, prompt_tokens=100, completion_tokens=10,
                cache_read_tokens=read, cache_write_tokens=0,
            )
            assert price.known
            assert price.micro_usd == round((100 - read) * expected[0] + 10 * expected[1] + read * expected[2])
            frozen = book.snapshot_token_prices(model.id, deployment=option.deploymentName)
            bound = frozen.estimate_token_bound(model.id, prompt_tokens=100, completion_tokens=10)
            assert bound.micro_usd == round(100 * expected[0] + 10 * expected[1])
            assert bound.micro_usd >= price.micro_usd
            assert not book.estimate(
                model.id, deployment="unknown-eastus2-deployment", prompt_tokens=100, completion_tokens=10,
                cache_read_tokens=read, cache_write_tokens=0,
            ).known
        assert book.rate(model.id) is None


def test_uncertain_cache_write_duration_and_lost_breakdown_remain_unknown():
    book = load_pricing()
    model = entries()[0]
    deployment = model.options[0].deploymentName
    for write in (0, 1):
        usage = TokenUsage.parse(anthropic_usage_to_chat({
            "input_tokens": 10, "cache_creation_input_tokens": write, "cache_read_input_tokens": 2, "output_tokens": 4,
        }))
        result = book.estimate(
            model.id, deployment=deployment, prompt_tokens=usage.prompt, completion_tokens=usage.completion,
            cache_read_tokens=usage.cacheRead, cache_write_tokens=usage.cacheWrite,
        )
        assert result.known is (write == 0)
        restored = TokenUsage.model_validate(usage.model_dump(mode="json"))
        assert restored.cacheRead is None and restored.cacheWrite is None
        assert not book.estimate(
            model.id, deployment=deployment, prompt_tokens=restored.prompt, completion_tokens=restored.completion,
            cache_read_tokens=restored.cacheRead, cache_write_tokens=restored.cacheWrite,
        ).known
    aggregate = TokenUsage.empty().add(TokenUsage.parse(anthropic_usage_to_chat({
        "input_tokens": 10, "cache_read_input_tokens": 2, "output_tokens": 4,
    }))).add(TokenUsage.parse(anthropic_usage_to_chat({
        "input_tokens": 5, "cache_read_input_tokens": 3, "output_tokens": 4,
    })))
    assert (aggregate.prompt, aggregate.cacheRead, aggregate.cacheWrite) == (20, 5, 0)


async def test_real_gateway_freezes_deployment_rates_before_provider_await(monkeypatch):
    model = entries()[0]
    deployment = next(option for option in model.options if option.sku == "DataZoneStandard").deploymentName
    book = load_pricing()

    def handler(_request):
        monkeypatch.setitem(book._scoped_rates[model.id], deployment, PriceRate(99, 199, 9))
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "synthetic"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 2, "output_tokens": 4},
        })

    recorder = ModelCallRecorder(model_id=model.id, deployment=deployment, pricing=book)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        gateway = ModelGatewayClient(make_settings(claude_enabled=True, claude_external_enabled=True), http_client=http)
        await recorder.observe(gateway.complete(
            deployment=deployment, messages=[{"role": "user", "content": "synthetic"}], api="anthropic",
        ))
    snapshot = recorder.snapshot()[0]
    assert snapshot.cost.priceInputPer1M == 5.5
    assert snapshot.cost.priceOutputPer1M == 27.5
    assert snapshot.cost.estCostMicroUsd == round(10 * 5.5 + 2 * 0.55 + 4 * 27.5)
    assert book.snapshot_token_prices(model.id, deployment=deployment).rate(model.id).input_per_1m == 99
    assert recorder.snapshot()[0] == snapshot
    assert load_pricing().rate("claude-opus-4-8") == PriceRate(5, 25)
