"""Catalog-owned controls must survive adaptation on the actual provider surface."""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from ai4ia_api.catalog import ModelEntry, _transform_infra_models, load_catalog
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.routers.chat import _effective_params, _prompt_byte_budget

from .conftest import make_settings
from tests.test_agent_runtime import _assistant_tool_call
from tests.test_model_call_evidence import receipts, transport_client
from ai4ia_api.usage.pricing import PriceRate, PricingBook


def _profile(**overrides):
    return {
        "id": "fixture-modern",
        "displayName": "Fixture modern",
        "category": "reasoning",
        "format": "fixture",
        "api": "chat",
        "contextWindow": 500_000,
        "maxOutputTokens": 500_000,
        "defaultMaxOutputTokens": 16_384,
        "maxTokensParameter": "max_completion_tokens",
        "samplingSupported": False,
        "reasoningEffort": ["low", "medium", "high", "xhigh"],
        "options": [{
            "region": "eastus2", "sku": "GlobalStandard",
            "deploymentName": "fixture-modern-deployment", "modelVersion": "1",
        }],
        **overrides,
    }


def _gateway(tmp_path, **overrides):
    raw = _profile(**overrides)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"models": [raw]}), encoding="utf-8")
    settings = make_settings(model_gateway_url="https://proxy.example.test/openai", model_catalog_path=str(path))
    return ModelEntry.model_validate(raw), ModelGatewayClient(settings)


def test_full_context_output_ceiling_keeps_a_usable_application_default(tmp_path):
    profile, _ = _gateway(tmp_path)
    params = _effective_params({}, profile)
    assert params["max_tokens"] == 16_384
    assert profile.maxOutputTokens == 500_000
    assert _prompt_byte_budget(profile, params) > 470_000
    assert _effective_params({"max_tokens": 1024}, profile)["max_tokens"] == 16_384
    assert _effective_params({"max_tokens": 2000}, profile)["max_tokens"] == 2000
    assert _effective_params({"max_tokens": 600_000}, profile)["max_tokens"] == 500_000


def test_chat_token_spelling_and_sampling_come_from_the_exact_catalog_profile(tmp_path):
    profile, gateway = _gateway(tmp_path)
    supplied = {"max_tokens": 2048, "temperature": 0.4, "top_p": 0.8, "reasoning_effort": "xhigh"}
    request = gateway.build_request(
        deployment=profile.options[0].deploymentName, messages=[], params=supplied,
    )
    assert request.json["max_completion_tokens"] == 2048
    assert "max_tokens" not in request.json
    assert "temperature" not in request.json and "top_p" not in request.json
    assert request.json["reasoning_effort"] == "xhigh"
    assert supplied["temperature"] == 0.4 and supplied["max_tokens"] == 2048


def test_ordinary_chat_profile_preserves_sampling_and_legacy_token_spelling(tmp_path):
    profile, gateway = _gateway(tmp_path, samplingSupported=True, maxTokensParameter="max_tokens")
    request = gateway.build_request(
        deployment=profile.options[0].deploymentName, messages=[],
        params={"max_tokens": 2048, "temperature": 0.4, "top_p": 0.8},
    )
    assert request.json["max_tokens"] == 2048
    assert "max_completion_tokens" not in request.json
    assert (request.json["temperature"], request.json["top_p"]) == (0.4, 0.8)


@pytest.mark.parametrize("effort", ["none", "low", None, "stale"])
@pytest.mark.parametrize("stream", [False, True])
def test_responses_sampling_requires_an_explicit_supported_none_effort(tmp_path, effort, stream):
    profile, gateway = _gateway(
        tmp_path, api="responses", maxTokensParameter=None, samplingSupported=True,
        samplingRequiresNoReasoning=True, reasoningEffort=["none", "low", "high"],
    )
    params = {"max_tokens": 2048, "temperature": 0.4, "top_p": 0.8}
    if effort is not None:
        params["reasoning_effort"] = effort
    request = gateway.build_responses_request(
        deployment=profile.options[0].deploymentName, messages=[],
        params=_effective_params(params, profile), stream=stream,
    )
    assert ("temperature" in request.json) is (effort == "none")
    assert ("top_p" in request.json) is (effort == "none")
    if effort == "none":
        assert (request.json["temperature"], request.json["top_p"]) == (0.4, 0.8)
    assert request.json["store"] is False
    assert request.json["max_output_tokens"] == 16_384


@pytest.mark.parametrize("stream", [False, True])
async def test_profile_adaptation_reaches_http_and_sse_without_a_second_dispatch(tmp_path, stream):
    raw = _profile()
    path = tmp_path / "wire-catalog.json"
    path.write_text(json.dumps({"models": [raw]}), encoding="utf-8")
    seen = []

    def provider(request):
        seen.append(json.loads(request.content))
        if stream:
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        gateway = ModelGatewayClient(
            make_settings(model_gateway_url="https://proxy.example.test/openai", model_catalog_path=str(path)),
            http_client=http,
        )
        args = {
            "deployment": raw["options"][0]["deploymentName"],
            "messages": [{"role": "user", "content": "synthetic"}],
            "params": {"max_tokens": 2048, "reasoning_effort": "low", "temperature": 0.4},
        }
        if stream:
            assert "".join(chunk.delta for chunk in [c async for c in gateway.stream(**args)]) == "ok"
        else:
            assert (await gateway.complete(**args))["choices"][0]["message"]["content"] == "ok"
    assert len(seen) == 1
    assert seen[0]["max_completion_tokens"] == 2048
    assert "max_tokens" not in seen[0] and "temperature" not in seen[0]


@pytest.mark.parametrize("changes", [
    {"defaultMaxOutputTokens": 500_001},
    {"defaultMaxOutputTokens": 500_000},
    {"defaultMaxOutputTokens": True},
    {"maxTokensParameter": "caller_selected"},
    {"samplingRequiresNoReasoning": True},
    {"api": "responses", "maxTokensParameter": None, "samplingSupported": True,
     "samplingRequiresNoReasoning": True, "reasoningEffort": ["low"]},
    {"api": "responses", "maxTokensParameter": None, "samplingSupported": True,
     "samplingRequiresNoReasoning": "true", "reasoningEffort": ["none"]},
])
def test_inconsistent_parameter_profiles_refuse_loading(changes):
    with pytest.raises(ValidationError):
        ModelEntry.model_validate(_profile(**changes))


def test_generated_and_development_catalogs_keep_the_same_parameter_contract():
    raw = _profile()
    source = {
        "naming": {"subscriptionToken": "fixture", "skuShort": {"GlobalStandard": "glbl"}},
        "regions": {"eastus2": {"dataZone": "US"}},
        "catalog": [{
            **{key: value for key, value in raw.items() if key not in {"id", "options"}},
            "name": raw["id"],
            "deployments": [{"region": "eastus2", "sku": "GlobalStandard", "version": "1", "capacity": 10}],
        }],
    }
    transformed = _transform_infra_models(source)["models"][0]
    profile = ModelEntry.model_validate(transformed)
    assert profile.maxTokensParameter == "max_completion_tokens"
    assert profile.defaultMaxOutputTokens == 16_384
    assert profile.samplingSupported is False


def test_october_additions_have_exact_distinct_provider_profiles():
    catalog = load_catalog()
    sol, grok = catalog.get("gpt-6.1-sol"), catalog.get("grok-4.7")
    assert sol is not None and grok is not None
    assert catalog.conversational_models()[0].id == "gpt-6-astra"
    assert (sol.api, sol.contextWindow, sol.maxOutputTokens) == ("responses", 1_050_000, 128_000)
    assert sol.reasoningEffortOptions == ["low", "medium", "high", "xhigh", "max"]
    assert sol.supportsSampling is False and sol.supportsTools is True
    assert {option.modelVersion for option in sol.options} == {"2026-09-29"}
    assert (grok.api, grok.contextWindow, grok.maxOutputTokens) == ("chat", 500_000, 500_000)
    assert grok.defaultMaxOutputTokens == 16_384 and grok.maxTokensParameter == "max_completion_tokens"
    assert grok.reasoningEffortOptions == ["low", "medium", "high", "xhigh"]
    assert grok.supportsSampling is False and grok.supportsTools is True
    assert {option.modelVersion for option in grok.options} == {"1"}
    assert [(option.region, option.sku) for option in grok.options] == [
        ("eastus2", "GlobalStandard"), ("swedencentral", "GlobalStandard"),
    ]


def test_refreshed_gpt_efforts_preserve_live_supported_legacy_values():
    catalog = load_catalog()
    for model_id in ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        entry = catalog.get(model_id)
        assert entry is not None and "max" in entry.reasoningEffortOptions
        assert ("none" in entry.reasoningEffortOptions) is (model_id != "gpt-6-astra")
    legacy = catalog.get("gpt-5.4")
    assert legacy is not None and "minimal" in legacy.reasoningEffortOptions


@pytest.mark.parametrize("model_id", ["gpt-6.1-sol", "grok-4.7"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tool_loop", [False, True])
def test_added_profiles_drive_real_application_tool_loops_and_durable_receipts(model_id, stream, tool_loop):
    entry = load_catalog().get(model_id)
    assert entry is not None
    option = entry.options[0]

    def provider(body, iteration, result):
        invoke = tool_loop and iteration == 1
        if entry.api == "chat":
            result["model"] = model_id
            if invoke:
                result["choices"] = _assistant_tool_call(
                    "fixture-calculator", "calculator", '{"expression":"6*7"}',
                )["choices"]
            return result
        item = {
            "type": "function_call", "id": "fc-fixture", "call_id": "fixture-calculator",
            "name": "calculator", "arguments": '{"expression":"6*7"}',
        } if invoke else {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "Done."}],
        }
        response = {
            "id": f"response-{iteration}", "object": "response", "status": "completed",
            "model": model_id, "output": [item],
            "usage": {"input_tokens": 1000, "output_tokens": 250, "total_tokens": 1250},
        }
        if not body.get("stream"):
            return httpx.Response(200, json=response)
        events = [{
            "type": "response.created", "response": {"id": response["id"], "model": model_id},
        }, {
            "type": "response.output_item.done", "output_index": 0, "item": item,
        } if invoke else {
            "type": "response.output_text.delta", "delta": "Done.",
        }, {
            "type": "response.completed",
            "response": {key: value for key, value in response.items() if key != "output"},
        }]
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            text="".join("data: " + json.dumps(event) + "\n\n" for event in events),
        )

    with transport_client(respond=provider) as (client, requests):
        client.app.state.usage._pricing = PricingBook(
            {model_id: PriceRate(2, 10 if entry.api == "responses" else 6)},
            currency="USD", version="new-profile-frozen-prices",
        )
        created = client.post("/api/sessions", json={"model": model_id})
        assert created.status_code == 201, created.text
        sid = created.json()["id"]
        outcome = client.post("/api/chat", json={
            "sessionId": sid, "content": "@analyst Calculate 6*7" if tool_loop else "Hello",
            "stream": stream, "allowTools": tool_loop, "allowAutomaticMemory": False,
            "params": {"reasoning_effort": "max" if entry.api == "responses" else "xhigh"},
        })
        assert outcome.status_code == 200, outcome.text
        saved = receipts(client, sid)[0]
        assert saved["status"] == "complete"
        assert saved["toolCallCount"] == (1 if tool_loop else 0)
        assert len(requests) == len(saved["runtime"]["modelCalls"]) == (2 if tool_loop else 1)
        for wire, call in zip(requests, saved["runtime"]["modelCalls"], strict=True):
            field = "max_output_tokens" if entry.api == "responses" else "max_completion_tokens"
            assert wire[field] == (128_000 if entry.api == "responses" else 16_384)
            assert call["parameters"]["outputTokenField"] == field
            assert call["parameters"]["reasoningEffort"] == ("max" if entry.api == "responses" else "xhigh")
            assert call["responseModel"] == {"status": "reported", "modelId": model_id, "version": None}
            assert call["cost"]["priceVersion"] == "new-profile-frozen-prices"
        assert saved["runtime"]["deployment"] == option.deploymentName
        assert receipts(client, sid)[0] == saved
