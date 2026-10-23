"""Exact selected-model profiles, native transports and per-call context pricing."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from ai4ia_api.catalog import ModelEntry, _transform_infra_models, load_catalog
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.model_evidence import ModelCallRecorder
from ai4ia_api.usage.models import TokenUsage
from ai4ia_api.usage.pricing import ContextPriceRate, PriceRate, load_pricing
from tests.conftest import make_settings
from tests.test_claude_adaptive_api import Provider, captured_logs, claude_client
from tests.test_external_claude import _TOOL_SURFACES
from tests.test_model_call_evidence import transport_response

SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
FLASH = "DeepSeek-V4-Flash"
HIDDEN = "PRIVATE-REASONING-DO-NOT-RETAIN"
VISIBLE = "The synthetic result is 42."
ROOT = Path(__file__).resolve().parents[3]


def test_selected_catalog_profiles_and_generated_development_parity():
    catalog = load_catalog()
    source = json.loads((ROOT / "infra" / "models.json").read_text())
    transformed = {m["id"]: ModelEntry.model_validate(m) for m in _transform_infra_models(source)["models"]}
    for model_id, thinking, tools, default in (
        (SONNET, "adaptive", False, "high"), (HAIKU, "disabled", True, "medium"),
    ):
        model = catalog.get(model_id)
        assert model is not None and model == transformed[model_id]
        assert model.deploymentTarget == "external-claude" and model.api == "anthropic"
        assert (model.anthropicThinking, model.supportsTools, model.defaultReasoningEffort) == (
            thinking, tools, default,
        )
        assert (model.contextWindow, model.maxOutputTokens) == (1_000_000, 128_000)
        assert model.reasoningEffortOptions == ["low", "medium", "high"]
        assert model.inputModalities == ["text"] and not model.supportsSampling
        assert {(o.region, o.sku, o.modelVersion) for o in model.options} == {
            ("eastus2", "GlobalStandard", "2"), ("eastus2", "DataZoneStandard", "2"),
        }
    flash = catalog.get(FLASH)
    assert flash is not None and flash == transformed[FLASH]
    assert flash.deploymentTarget == "source" and flash.api == "chat"
    assert not flash.supportsTools and not flash.supportsSampling
    assert flash.reasoningEffortOptions == [] and flash.inputModalities == ["text"]
    assert (flash.contextWindow, flash.maxOutputTokens, flash.defaultMaxOutputTokens) == (
        1_000_000, 384_000, 2_048,
    )
    assert next(m for m in source["catalog"] if m["name"] == FLASH)["deployments"] == [
        {"region": "eastus2", "sku": "GlobalStandard", "capacity": 10, "version": "2026-04-23"},
    ]
    for policy in ("global", "us", "eu"):
        governed = catalog.model_copy(update={"residencyPolicy": policy})
        assert (governed.resolve_deployment(FLASH) is not None) is (policy == "global")
        for model_id in (SONNET, HAIKU):
            option = governed.resolve_deployment(model_id)
            assert (option is not None) is (policy != "eu")
            if policy == "us":
                assert option.sku == "DataZoneStandard"


@pytest.mark.parametrize("changes", [
    {"defaultReasoningEffort": "max"},
    {"defaultReasoningEffort": "low", "reasoningEffort": ["medium", "high"]},
    {"deploymentTarget": "source"},
])
def test_invalid_explicit_default_never_becomes_a_provider_fallback(changes):
    row = load_catalog().get(SONNET).model_dump()
    ModelEntry.model_validate(row)
    with pytest.raises(ValidationError):
        ModelEntry.model_validate({**row, **changes})


@pytest.mark.parametrize("sku,multiplier", [("GlobalStandard", "1"), ("DataZoneStandard", "1.1")])
@pytest.mark.parametrize("prompt", [99_999, 100_000, 100_001, 1_000_000])
@pytest.mark.parametrize("cached", [0, 80_000])
def test_haiku_threshold_counts_cached_input_and_prices_the_whole_call(sku, multiplier, prompt, cached):
    option = next(o for o in load_catalog().get(HAIKU).options if o.sku == sku)
    book = load_pricing()
    assert book.rate(HAIKU, deployment=option.deploymentName) is None
    rates = ("0.1", "0.5", "0.01") if prompt <= 100_000 else ("0.5", "2.5", "0.05")
    inp, out, hit = [Decimal(rate) * Decimal(multiplier) for rate in rates]
    for priced, deployment in ((book, option.deploymentName), (
        book.snapshot_token_prices(HAIKU, deployment=option.deploymentName), None,
    )):
        estimate = priced.estimate(
            HAIKU, deployment=deployment, prompt_tokens=prompt, completion_tokens=20,
            cache_read_tokens=cached, cache_write_tokens=0,
        )
        assert estimate.known
        assert (estimate.input_per_1m, estimate.output_per_1m) == (float(inp), float(out))
        assert estimate.micro_usd == round((prompt - cached) * inp + cached * hit + 20 * out)
        assert priced.rate(HAIKU, deployment=deployment, prompt_tokens=prompt) == PriceRate(
            float(inp), float(out), float(hit),
        )


@pytest.mark.parametrize("change", [
    {"prompt_tokens": None}, {"cache_read_tokens": None}, {"cache_write_tokens": None},
    {"cache_write_tokens": 1}, {"cache_read_tokens": 100_002},
    {"total_tokens": 1}, {"totals_mismatch": True}, {"calls": 2},
    {"deployment": "unknown"}, {"prompt_tokens": -1}, {"prompt_tokens": True},
])
def test_haiku_missing_or_aggregate_evidence_is_unknown(change):
    deployment = load_catalog().get(HAIKU).options[0].deploymentName
    args = dict(
        deployment=deployment, prompt_tokens=100_001, completion_tokens=20,
        cache_read_tokens=80_000, cache_write_tokens=0,
    )
    assert load_pricing().estimate(HAIKU, **args).known
    estimate = load_pricing().estimate(HAIKU, **{**args, **change})
    assert not estimate.known and estimate.micro_usd is None


def test_haiku_ledger_does_not_reprice_aggregated_prompts_as_a_single_long_call():
    with claude_client(gateway_mode=True) as (client, _):
        service = client.app.state.usage
        model = load_catalog().get(HAIKU)
        usage = TokenUsage(
            prompt=100_001, completion=20, total=100_021, calls=1,
            known=True, complete=True, cacheRead=80_000, cacheWrite=0,
        )
        args = dict(
            user_id="synthetic", session_id="synthetic", model_id=HAIKU,
            deployment=model.options[0], status="complete", agent=None, correlation_id=None,
        )
        assert service.build_record(**args, usage=usage).costKnown
        aggregate = usage.model_copy(update={"calls": 2})
        record = service.build_record(**args, usage=aggregate)
        assert not record.costKnown and record.estCostMicroUsd is None


def test_flash_exact_price_schedule_requires_cache_evidence():
    option = load_catalog().get(FLASH).options[0]
    book = load_pricing()
    assert book.rate(FLASH) is None
    assert book.rate(FLASH, deployment=option.deploymentName) == PriceRate(0.19, 0.51, 0.028)
    args = dict(deployment=option.deploymentName, prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert not book.estimate(FLASH, **args).known
    priced = book.estimate(FLASH, **args, cache_read_tokens=500_000, cache_write_tokens=0)
    assert priced.known and priced.micro_usd == 619_000


async def test_haiku_freezes_both_tiers_before_the_provider_await(monkeypatch):
    model = load_catalog().get(HAIKU)
    deployment = model.options[0].deploymentName
    book = load_pricing()
    recorder = ModelCallRecorder(model_id=HAIKU, deployment=deployment, pricing=book)

    def respond(_request):
        monkeypatch.setitem(book._scoped_rates[HAIKU], deployment, ContextPriceRate(
            1, PriceRate(99, 99, 99), PriceRate(199, 199, 199),
        ))
        return httpx.Response(200, json={
            "model": HAIKU, "content": [{"type": "text", "text": VISIBLE}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 20_001, "cache_read_input_tokens": 80_000, "output_tokens": 20},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        gateway = ModelGatewayClient(make_settings(claude_enabled=True, claude_gateway_enabled=True), http_client=http)
        await recorder.observe(gateway.complete(
            deployment=deployment, messages=[{"role": "user", "content": "synthetic"}],
        ))
    evidence = recorder.snapshot()[0]
    assert (evidence.cost.priceInputPer1M, evidence.cost.priceOutputPer1M) == (0.5, 2.5)
    assert evidence.cost.estCostMicroUsd == round(20_001 * 0.5 + 80_000 * 0.05 + 20 * 2.5)
    assert evidence.cost.priceVersion == "2026-10-23-selected"
    assert recorder.snapshot()[0] == evidence


class SelectedProvider(Provider):
    def __init__(self, model_id: str, *, tool_loop: bool = False):
        super().__init__()
        self.model_id = model_id
        self.tool_loop = tool_loop
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.claude.append(body)
        self.urls.append(str(request.url))
        tool = self.tool_loop and len(self.claude) == 1
        if self.model_id == FLASH:
            return transport_response(body, {
                "model": FLASH,
                "choices": [{"message": {"role": "assistant", "content": VISIBLE, "reasoning_content": HIDDEN}}],
                "usage": {
                    "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 40},
                    "completion_tokens_details": {"reasoning_tokens": 10},
                },
            })
        block = {
            "type": "tool_use", "id": "synthetic-calculator", "name": "calculator",
            "input": {"expression": "6*7"},
        } if tool else {"type": "text", "text": VISIBLE}
        usage = {
            "input_tokens": 20_000 if tool else 20_001,
            "cache_read_input_tokens": 80_000, "output_tokens": 20,
        }
        message = {
            "model": self.model_id, "type": "message", "role": "assistant",
            "content": [{"type": "thinking", "thinking": HIDDEN, "signature": HIDDEN}, block],
            "stop_reason": "tool_use" if tool else "end_turn", "usage": usage,
        }
        if not body.get("stream"):
            return httpx.Response(200, json=message)
        events = [
            {"type": "message_start", "message": {"model": self.model_id, "usage": {
                **usage, "output_tokens": 0,
            }}},
            {"type": "content_block_start", "index": 0, "content_block": message["content"][0]},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {
                **block, **({"input": {}} if tool else {"text": ""}),
            }},
            {"type": "content_block_delta", "index": 1, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(block["input"]),
            } if tool else {"type": "text_delta", "text": VISIBLE}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": message["stop_reason"]}, "usage": {"output_tokens": 20}},
            {"type": "message_stop"},
        ]
        return httpx.Response(200, text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
                              headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("params,expected", [
    ({}, 2048),
    ({"max_tokens": 1024}, 2048),
    ({"max_tokens": 1500}, 1500),
    ({"max_tokens": 4096}, 4096),
])
def test_flash_ordinary_browser_default_and_explicit_limits_reach_native_request(stream, params, expected):
    provider = SelectedProvider(FLASH)
    with claude_client(provider=provider, gateway_mode=True) as (client, _):
        sid = client.post("/api/sessions", json={"model": FLASH}).json()["id"]
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "Synthetic default control", "stream": stream,
            "allowTools": False, "allowAutomaticMemory": False, "params": params,
        })
        assert response.status_code == 200, response.text
        [wire] = provider.claude
        assert wire["max_tokens"] == expected and "max_completion_tokens" not in wire
        saved = client.get(f"/api/sessions/{sid}/messages").json()[-1]
        assert saved["executionReceipt"]["runtime"]["modelCalls"][0]["parameters"]["maxOutputTokens"] == expected
        assert client.delete(f"/api/sessions/{sid}").status_code == 204


@pytest.mark.parametrize("stream", [False, True])
async def test_flash_gateway_drops_reasoning_before_any_consumer(stream):
    provider = SelectedProvider(FLASH)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        gateway = ModelGatewayClient(make_settings(), http_client=http)
        args = dict(
            deployment=load_catalog().get(FLASH).options[0].deploymentName,
            messages=[{"role": "user", "content": "synthetic"}],
        )
        if stream:
            chunks = [chunk async for chunk in gateway.stream(**args)]
            delivered = "".join(chunk.raw for chunk in chunks)
            assert "".join(chunk.delta for chunk in chunks) == VISIBLE
            assert next(chunk.usage for chunk in chunks if chunk.usage)["total_tokens"] == 120
            usage = next(chunk.usage for chunk in chunks if chunk.usage)
        else:
            result = await gateway.complete(**args)
            delivered = json.dumps(result)
            assert result["choices"][0]["message"]["content"] == VISIBLE
            assert result["usage"]["total_tokens"] == 120
            usage = result["usage"]
        assert FLASH in delivered and HIDDEN not in delivered
        native = TokenUsage.parse(usage).nativeUsage.calls[0]
        assert native.values["completion_tokens_details.reasoning_tokens"] == 10
        assert native.values["prompt_tokens_details.cached_tokens"] == 40


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model_id", [SONNET, HAIKU, FLASH])
def test_selected_models_use_app_path_and_hide_reasoning_with_native_model_usage(model_id, stream, caplog):
    provider = SelectedProvider(model_id)
    with claude_client(provider=provider, gateway_mode=True) as (client, _), captured_logs(caplog):
        created = client.post("/api/sessions", json={"model": model_id})
        assert created.status_code == 201
        sid = created.json()["id"]
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "Synthetic only", "stream": stream,
            "allowTools": False, "allowAutomaticMemory": False,
            "params": {"max_tokens": 2048},
        })
        assert response.status_code == 200, response.text
        messages = client.get(f"/api/sessions/{sid}/messages").json()
        assert messages[-1]["content"] == VISIBLE and VISIBLE in response.text
        call = messages[-1]["executionReceipt"]["runtime"]["modelCalls"][0]
        assert call["responseModel"]["status"] == "reported"
        assert call["responseModel"]["modelId"] == model_id
        assert call["promptTokens"] == (100 if model_id == FLASH else 100_001)
        assert call["completionTokens"] == 20
        assert call["cost"]["coverage"] == ("unknown" if model_id == FLASH else "known")
        if model_id == FLASH:
            native = call["nativeUsage"]
            assert native["api"] == "chat" and native["status"] == "reported"
            assert native["values"] == {
                "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                "prompt_tokens_details.cached_tokens": 40,
                "completion_tokens_details.reasoning_tokens": 10,
            }
        if model_id == HAIKU:
            assert (call["cost"]["priceInputPer1M"], call["cost"]["priceOutputPer1M"]) == (0.5, 2.5)
        for surface in (response.text, json.dumps(messages), caplog.text):
            assert HIDDEN not in surface
        assert client.delete(f"/api/sessions/{sid}").status_code == 204
    [wire] = provider.claude
    assert len(provider.urls) == 1 and provider.urls[0].startswith("https://proxy.test/openai/deployments/")
    assert not {"tools", "tool_choice", "temperature", "top_p", "reasoning_content"} & wire.keys()
    assert wire["max_tokens"] == 2048 and "max_completion_tokens" not in wire
    if model_id == FLASH:
        assert "reasoning_effort" not in wire
    else:
        assert wire["output_config"] == {"effort": "high" if model_id == SONNET else "medium"}
        assert ("thinking" in wire) is (model_id == HAIKU)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model_id", [SONNET, HAIKU, FLASH])
def test_only_haiku_can_enter_native_tool_loop_and_each_call_owns_its_tier(model_id, stream):
    provider = SelectedProvider(model_id, tool_loop=True)
    with claude_client(provider=provider, gateway_mode=True) as (client, _):
        agent = client.post("/api/agents", json={
            "name": "selected-calc", "systemPrompt": "Use calculator.", "tools": ["calculator"],
        })
        assert agent.status_code == 201
        sid = client.post("/api/sessions", json={"model": model_id}).json()["id"]
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "@selected-calc Calculate 6*7", "stream": stream,
            "allowAutomaticMemory": False,
        })
        messages = client.get(f"/api/sessions/{sid}/messages").json()
        if model_id != HAIKU:
            assert response.status_code == 422 and provider.claude == [] and messages == []
        else:
            assert response.status_code == 200, response.text
            assert len(provider.claude) == 2
            assert all(body["thinking"] == {"type": "disabled"} for body in provider.claude)
            history = provider.claude[1]["messages"]
            assert any(block["type"] == "tool_result" for msg in history for block in msg["content"])
            assert HIDDEN not in json.dumps(history) + json.dumps(messages) + response.text
            calls = messages[-1]["executionReceipt"]["runtime"]["modelCalls"]
            assert [(c["cost"]["priceInputPer1M"], c["cost"]["priceOutputPer1M"]) for c in calls] == [
                (0.1, 0.5), (0.5, 2.5),
            ]
            assert messages[-1]["content"] == VISIBLE
        assert client.delete(f"/api/sessions/{sid}").status_code == 204


@pytest.mark.parametrize("stream", [False, True])
async def test_flash_refuses_tools_and_tool_history_at_actual_dispatch(stream):
    requests = []

    def respond(request):
        requests.append(request)
        return transport_response(json.loads(request.content), {
            "choices": [{"message": {"content": VISIBLE}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        gateway = ModelGatewayClient(make_settings(), http_client=http)
        deployment = load_catalog().get(FLASH).options[0].deploymentName

        async def invoke(params, messages):
            args = dict(deployment=deployment, messages=messages, params=params)
            return [chunk async for chunk in gateway.stream(**args)] if stream else await gateway.complete(**args)

        for params, messages in _TOOL_SURFACES:
            with pytest.raises(ValueError, match="text-only"):
                await invoke(params, messages)
        assert not requests
        assert await invoke({}, [{"role": "user", "content": "synthetic"}])
        assert len(requests) == 1
