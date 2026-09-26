"""Actual Claude payloads, server gates and deployment-aware frozen prices."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ai4ia_api.agents.consent import contract_hash
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


def disabled_entries():
    return [m for m in entries() if m.anthropicThinking == "disabled"]


def adaptive_entries():
    return [m for m in entries() if m.anthropicThinking == "adaptive"]


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_supported_profile_reaches_native_payload_and_receipt(effort):
    gateway = ModelGatewayClient(make_settings(claude_enabled=True, claude_external_enabled=True))
    for model in disabled_entries():
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
            for model in disabled_entries():
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
            disabled = [m for m in offered if m["anthropicThinking"] == "disabled"]
            assert len(disabled) == (2 if enabled else 0)
            assert all(m["reasoningEffortOptions"] == ["low", "medium", "high"] for m in offered)
            assert {m["id"]: (m["anthropicThinking"], m["supportsTools"]) for m in offered if m not in disabled} == (
                {"claude-opus-5-5": ("adaptive", False)} if enabled else {}
            )
            assert all(m["supportsTools"] is True for m in disabled)


@pytest.mark.parametrize("read", [0, 20])
def test_exact_sku_rates_include_cached_read_and_output_without_region_guessing(read):
    book = load_pricing()
    expected_rates = {
        ("claude-opus-5", "GlobalStandard"): (5.0, 25.0, 0.5),
        ("claude-opus-5", "DataZoneStandard"): (5.5, 27.5, 0.55),
        ("claude-sonnet-5", "GlobalStandard"): (2.0, 10.0, 0.2),
        ("claude-sonnet-5", "DataZoneStandard"): (2.2, 11.0, 0.22),
        # Opus 5.5 cache hits are 0.05x input, not the 0.1x of its siblings.
        ("claude-opus-5-5", "GlobalStandard"): (4.0, 20.0, 0.2),
        ("claude-opus-5-5", "DataZoneStandard"): (4.4, 22.0, 0.22),
    }
    for model in entries():
        for option in model.options:
            expected = expected_rates[(model.id, option.sku)]
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


_USER = [{"role": "user", "content": "synthetic"}]
_TOOL = {"type": "function", "function": {"name": "calculator", "parameters": {"type": "object", "properties": {}}}}
_CALL = {"id": "call_1", "type": "function", "function": {"name": "calculator", "arguments": "{}"}}
# Each is a tool surface the adaptive profile must refuse before any byte leaves.
_TOOL_SURFACES = [
    ({"tools": [_TOOL]}, _USER),
    ({"tools": [_TOOL], "tool_choice": "auto"}, _USER),
    ({"tool_choice": "required"}, _USER),
    ({"tool_choice": {"type": "function", "function": {"name": "calculator"}}}, _USER),
    ({}, [*_USER, {"role": "assistant", "content": "", "tool_calls": [_CALL]}]),
    ({}, [*_USER, {"role": "tool", "tool_call_id": "call_1", "content": "42"}]),
    ({}, [
        *_USER, {"role": "assistant", "content": "", "tool_calls": [_CALL]},
        {"role": "tool", "tool_call_id": "call_1", "content": "42"},
    ]),
]


@pytest.mark.parametrize("effort", ["low", "medium", "high", None])
def test_adaptive_profile_omits_thinking_and_records_effective_effort(effort):
    gateway = ModelGatewayClient(make_settings(claude_enabled=True, claude_external_enabled=True))
    params = {"temperature": 0.3, "top_p": 0.4, **({"reasoning_effort": effort} if effort else {})}
    assert adaptive_entries() and disabled_entries()
    for model in adaptive_entries():
        for option in model.options:
            body = gateway.build_anthropic_request(
                deployment=option.deploymentName, messages=_USER, params=params,
            ).json
            assert "thinking" not in body
            # Omitted effort sends Opus 5.5's documented default, never the disabled profile's high.
            assert body["output_config"] == {"effort": effort or "medium"}
            assert not {"temperature", "top_p", "tools", "tool_choice", "cache_control"} & body.keys()
            recorder = ModelCallRecorder(model_id=model.id, deployment=option.deploymentName, pricing=load_pricing())
            call = recorder.start(option.deploymentName, "anthropic")
            call.request(body)
            call.report_usage(anthropic_usage_to_chat({"input_tokens": 10, "output_tokens": 4}), completed=True)
            snapshot = call.snapshot()
            assert snapshot.parameters.reasoningEffort == (effort or "medium")
            assert snapshot.cost.coverage == "known"
            assert not supported_attempt_payload("chat", body)
    # Control: identical inputs on the thinking-disabled rows still disable thinking.
    for model in disabled_entries():
        body = gateway.build_anthropic_request(
            deployment=model.options[0].deploymentName, messages=_USER, params=params,
        ).json
        assert body["thinking"] == {"type": "disabled"}
        assert body["output_config"] == {"effort": effort or "high"}


async def test_adaptive_profile_refuses_every_tool_surface_before_dispatch():
    requests: list[dict] = []

    def handler(request):
        requests.append(json.loads(request.content))
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="".join(
                f"data: {json.dumps(event)}\n\n" for event in (
                    {"type": "message_start", "message": {"usage": {"input_tokens": 1, "output_tokens": 0}}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
                    {"type": "message_delta", "usage": {"output_tokens": 1}},
                    {"type": "message_stop"},
                )
            ))
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        gateway = ModelGatewayClient(make_settings(
            claude_enabled=True, claude_external_enabled=True, model_gateway_url="https://proxy.test/openai",
        ), http_client=http)
        [adaptive] = adaptive_entries()
        deployment = adaptive.options[0].deploymentName
        for params, messages in _TOOL_SURFACES:
            with pytest.raises(ValueError, match="adaptive text-only"):
                await gateway.complete(deployment=deployment, messages=messages, params=params, api="anthropic")
            with pytest.raises(ValueError, match="adaptive text-only"):
                async for _ in gateway.stream(deployment=deployment, messages=messages, params=params, api="anthropic"):
                    pass
        assert requests == []
        # Controls: text-only requests, including inert auto/none choices, dispatch once each.
        for params in ({}, {"tool_choice": "auto"}, {"tool_choice": "none"}, {"tools": []}):
            await gateway.complete(deployment=deployment, messages=_USER, params=params, api="anthropic")
        assert len(requests) == 4
        assert all(not {"tools", "tool_choice", "thinking"} & body.keys() for body in requests)
        # Control: the same tool surfaces are valid, translated input on the tool-capable profile.
        disabled = disabled_entries()[0].options[0].deploymentName
        for params, messages in _TOOL_SURFACES:
            body = gateway.build_anthropic_request(deployment=disabled, messages=messages, params=params).json
            assert body["thinking"] == {"type": "disabled"}
            if params.get("tools"):
                assert [tool["name"] for tool in body["tools"]] == ["calculator"]


@pytest.mark.parametrize(("target", "field", "value"), [
    ("adaptive", "toolCalling", True),
    ("adaptive", "toolCalling", None),
    ("adaptive", "reasoningEffort", ["low", "xhigh"]),
    ("adaptive", "reasoningEffort", []),
    ("adaptive", "samplingSupported", True),
    ("adaptive", "samplingSupported", None),
    ("adaptive", "inputModalities", ["text", "image"]),
    ("adaptive", "anthropicThinking", "enabled"),
    ("adaptive", "anthropicThinking", None),
    ("disabled", "toolCalling", False),
    ("disabled", "toolCalling", None),
])
def test_catalog_accepts_only_the_exact_disabled_and_adaptive_profiles(tmp_path, target, field, value):
    raw = json.loads((Path(__file__).parents[1] / "src" / "ai4ia_api" / "data" / "model_catalog.json").read_text())
    control = tmp_path / "control.json"
    control.write_text(json.dumps(raw))
    assert {m.anthropicThinking for m in load_catalog(str(control)).models if m.deploymentTarget == "external-claude"} == {
        "disabled", "adaptive",
    }
    row = next(m for m in raw["models"] if m.get("anthropicThinking") == target)
    if value is None:
        row.pop(field)
    else:
        row[field] = value
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_catalog(str(changed))


def test_adaptive_rows_cannot_move_default_off_or_ordinary_model_digests(tmp_path):
    raw = json.loads((Path(__file__).parents[1] / "src" / "ai4ia_api" / "data" / "model_catalog.json").read_text())
    without = deepcopy(raw)
    without["models"] = [m for m in raw["models"] if m.get("anthropicThinking") != "adaptive"]
    assert len(without["models"]) == len(raw["models"]) - 1
    paths = {}
    for name, document in (("with", raw), ("without", without)):
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(document))

    def digests(name, claude_enabled):
        return [contract_hash(model) for model in load_catalog(str(paths[name]), "global", claude_enabled).models]

    # Claude-disabled consent/publication environments hash no Anthropic row at all.
    assert digests("with", False) == digests("without", False)
    # Control: the enabled catalog does see the row, so the comparison is sensitive.
    assert digests("with", True) != digests("without", True)
    for model in load_catalog(str(paths["with"]), "global", True).models:
        dumped = model.model_dump(mode="json")
        if model.deploymentTarget == "external-claude":
            assert dumped["anthropicThinking"] in {"disabled", "adaptive"}
        else:
            assert not {"anthropicThinking", "deploymentTarget", "samplingSupported"} & dumped.keys(), model.id
