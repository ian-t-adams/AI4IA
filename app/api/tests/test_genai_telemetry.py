"""Capture complete spans and Azure wire envelopes, without starting an exporter."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ai4ia_api import logging_setup
from ai4ia_api.catalog import load_catalog
from ai4ia_api.gateway.client import ModelGatewayClient, ModelGatewayError
from ai4ia_api.genai_telemetry import ATTRIBUTE_NAMES, CONTRACT_VERSION, INSTRUMENTATION_NAME
from ai4ia_api.model_evidence import ModelCallRecorder

from .conftest import make_settings

POISON = "private-prompt-user-session-tool-secret-https://private.invalid"


@pytest.fixture
def capture(monkeypatch):
    provider = TracerProvider(resource=Resource({"service.name": "ai4ia-api"}))
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    monkeypatch.setattr(logging_setup, "_telemetry_configured", True)
    for key in (
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
        "AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING",
    ):
        monkeypatch.setenv(key, "true")
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST", ".*")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE", ".*")
    yield exporter, provider
    provider.shutdown()


def _model(api="chat"):
    catalog = load_catalog(None, "global", True)
    model = next(m for m in catalog.conversational_models() if m.api == api)
    return model, model.options[0].deploymentName


def _native(api, model):
    if api == "responses":
        return {
            "model": model, "status": "completed", "id": POISON,
            "output": [{"type": "message", "content": [{"type": "output_text", "text": POISON}]}],
            "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
        }
    if api == "anthropic":
        return {
            "model": model, "id": POISON, "type": "message",
            "content": [{"type": "text", "text": POISON}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 7, "cache_creation_input_tokens": 1,
                "cache_read_input_tokens": 2, "output_tokens": 3,
            },
        }
    return {
        "model": model, "id": POISON,
        "choices": [{"message": {"role": "assistant", "content": POISON}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    }


def _frames(api, model, *, done=True):
    body = _native(api, model)
    if api == "responses":
        events = [
            {"type": "response.created", "response": {"model": model, "id": POISON}},
            {"type": "response.output_text.delta", "delta": POISON},
        ]
        if done:
            events.append({"type": "response.completed", "response": body})
    elif api == "anthropic":
        events = [
            {"type": "message_start", "message": body},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": POISON}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
        ]
        if done:
            events.append({"type": "message_stop"})
    else:
        events = [
            {"model": model, "choices": [{"index": 0, "delta": {"content": POISON}}]},
            {"model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": body["usage"]},
            {"choices": [], "usage": body["usage"]},
        ]
    wire = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    if api == "chat" and done:
        wire += "data: [DONE]\n\n"
    return wire


def _gateway(http, *, enabled=True, include_usage=True):
    return ModelGatewayClient(make_settings(
        model_gateway_url=f"https://gateway.invalid/{POISON}",
        model_gateway_api_key=POISON, claude_enabled=True,
        applicationinsights_connection_string="InstrumentationKey=synthetic" if enabled else None,
        gateway_stream_include_usage=include_usage,
    ), http_client=http)


def _assert_clean(span):
    from azure.monitor.opentelemetry.exporter.export.trace._exporter import (
        _convert_span_events_to_envelopes, _convert_span_to_envelope,
    )

    assert span.instrumentation_scope.name == INSTRUMENTATION_NAME
    assert span.instrumentation_scope.version == CONTRACT_VERSION
    assert span.kind == trace.SpanKind.CLIENT
    assert set(span.attributes) <= ATTRIBUTE_NAMES
    assert span.events == () and span.links == ()
    assert span.status.description is None
    assert span.end_time >= span.start_time
    assert POISON not in span.to_json()
    assert _convert_span_events_to_envelopes(span) == []
    envelope = _convert_span_to_envelope(span)
    wire = json.dumps(envelope.as_dict())
    assert POISON not in wire
    assert envelope.data.base_data.data is None
    assert envelope.data.base_data.type == f"GenAI | {span.attributes['gen_ai.provider.name']}"
    assert span.attributes["gen_ai.provider.name"] == span.attributes["gen_ai.system"]


@pytest.mark.parametrize("api", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_actual_adapted_requests_and_native_returns_are_content_free(capture, api, stream):
    exporter, provider = capture
    model, deployment = _model(api)
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return (
            httpx.Response(200, text=_frames(api, model.id), headers={"x-private": POISON})
            if stream else httpx.Response(200, json=_native(api, model.id), headers={"x-private": POISON})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        HTTPXClientInstrumentor.instrument_client(http, tracer_provider=provider)
        gateway = _gateway(http)
        args = dict(
            deployment=deployment, api=api, messages=[{"role": "user", "content": POISON}],
            params={"max_tokens": 64, "temperature": 0.25, "top_p": 0.75, "stop": [POISON]},
            correlation_id=POISON,
        )
        if stream:
            chunks = [chunk async for chunk in gateway.stream(**args)]
            assert chunks[-1].done
        else:
            assert await gateway.complete(**args)
        HTTPXClientInstrumentor.uninstrument_client(http)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1  # No second, content-bearing HTTP dependency span.
    span = spans[0]
    _assert_clean(span)
    assert span.attributes["gen_ai.request.model"] == deployment
    assert span.attributes["gen_ai.response.model"] == model.id
    assert span.attributes["gen_ai.request.stream"] is stream
    expected = next(requests[0][key] for key in ("max_output_tokens", "max_completion_tokens", "max_tokens") if key in requests[0])
    assert span.attributes["gen_ai.request.max_tokens"] == expected
    for key, attribute in (("temperature", "gen_ai.request.temperature"), ("top_p", "gen_ai.request.top_p")):
        assert span.attributes.get(attribute) == requests[0].get(key)
    assert span.attributes["gen_ai.response.finish_reasons"] == (
        ("completed",) if api == "responses" else ("end_turn",) if api == "anthropic" else ("stop",)
    )
    assert span.attributes["gen_ai.usage.input_tokens"] == 10
    assert span.attributes["gen_ai.usage.output_tokens"] == 3
    assert span.attributes["ai4ia.gen_ai.usage.coverage"] == "known"


@pytest.mark.parametrize("failure,category", [
    ("timeout", "timeout"), (403, "authentication"), (429, "rate_limit"), (503, "provider"),
])
@pytest.mark.parametrize("stream", [False, True])
async def test_failed_calls_never_export_exception_text_or_fake_usage(capture, failure, category, stream):
    exporter, provider = capture
    _, deployment = _model()

    def handle(_request):
        if failure == "timeout":
            raise httpx.ReadTimeout(POISON)
        return httpx.Response(failure, text=POISON)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        HTTPXClientInstrumentor.instrument_client(http, tracer_provider=provider)
        gateway = _gateway(http)
        with pytest.raises(ModelGatewayError):
            if stream:
                _ = [chunk async for chunk in gateway.stream(
                    deployment=deployment, messages=[{"role": "user", "content": POISON}],
                )]
            else:
                await gateway.complete(deployment=deployment, messages=[{"role": "user", "content": POISON}])
        HTTPXClientInstrumentor.uninstrument_client(http)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    _assert_clean(spans[0])
    assert spans[0].status.status_code == trace.StatusCode.ERROR
    assert spans[0].attributes["error.type"] == category
    assert spans[0].attributes["ai4ia.gen_ai.usage.coverage"] == "unknown"
    assert "gen_ai.usage.input_tokens" not in spans[0].attributes


@pytest.mark.parametrize("raw", [
    None, {}, {"prompt_tokens": True, "completion_tokens": 3},
    {"prompt_tokens": -1, "completion_tokens": 3},
    {"prompt_tokens": 2**53, "completion_tokens": 3},
    {"prompt_tokens": 10, "completion_tokens": POISON},
    {"total_tokens": 13},
])
async def test_missing_or_invalid_usage_and_poisoned_metadata_stay_unknown(capture, raw):
    exporter, _ = capture
    model, deployment = _model()
    body = _native("chat", POISON)
    body["choices"][0]["finish_reason"] = POISON
    body["usage"] = raw
    body["gen_ai.input.messages"] = POISON
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as http:
        await _gateway(http).complete(deployment=deployment, messages=[])
    span = exporter.get_finished_spans()[0]
    _assert_clean(span)
    assert "gen_ai.response.model" not in span.attributes
    assert "gen_ai.response.finish_reasons" not in span.attributes
    assert "gen_ai.usage.input_tokens" not in span.attributes
    assert span.attributes["ai4ia.gen_ai.usage.coverage"] == "unknown"


async def test_span_uses_post_admission_payload_not_request_draft(capture, monkeypatch):
    from ai4ia_api.gateway import client as module

    exporter, _ = capture
    model, deployment = _model()

    @asynccontextmanager
    async def admit(_surface, payload, **_kwargs):
        adapted = dict(payload)
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            if key in adapted:
                adapted[key] = 13
        yield SimpleNamespace(payload=adapted, reservation=None)

    monkeypatch.setattr(module, "admitted_dispatch", admit)
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_native("chat", model.id))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await _gateway(http).complete(deployment=deployment, messages=[], params={"max_tokens": 200})
    span = exporter.get_finished_spans()[0]
    assert 13 in requests[0].values()
    assert span.attributes["gen_ai.request.max_tokens"] == 13


async def test_stream_retry_is_one_logical_span_and_never_sums_usage(capture):
    exporter, _ = capture
    model, deployment = _model()
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, text=POISON) if len(requests) == 1 else httpx.Response(200, text=_frames("chat", model.id))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        _ = [chunk async for chunk in _gateway(http).stream(deployment=deployment, messages=[])]
    spans = exporter.get_finished_spans()
    assert len(requests) == 2 and "stream_options" not in requests[1]
    assert len(spans) == 1
    _assert_clean(spans[0])
    assert spans[0].attributes["ai4ia.gen_ai.http_attempts"] == 2
    assert spans[0].attributes["gen_ai.usage.output_tokens"] == 3
    assert "error.type" not in spans[0].attributes


@pytest.mark.parametrize("api", ["chat", "responses", "anthropic"])
async def test_cross_task_stream_close_ends_span_without_leaking_context(capture, api):
    exporter, _ = capture
    model, deployment = _model(api)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text=_frames(api, model.id)),
    )) as http:
        stream = _gateway(http).stream(deployment=deployment, api=api, messages=[])
        assert await anext(stream)
        assert trace.get_current_span() is trace.INVALID_SPAN
        await asyncio.create_task(stream.aclose())
        assert trace.get_current_span() is trace.INVALID_SPAN
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    _assert_clean(spans[0])
    assert spans[0].attributes["error.type"] == "cancelled"
    assert spans[0].attributes["ai4ia.gen_ai.usage.coverage"] == "unknown"


@pytest.mark.parametrize("api", ["chat", "responses", "anthropic"])
async def test_terminal_chunk_close_preserves_completed_provider_usage(capture, api):
    exporter, _ = capture
    model, deployment = _model(api)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text=_frames(api, model.id)),
    )) as http:
        stream = _gateway(http).stream(deployment=deployment, api=api, messages=[])
        async for chunk in stream:
            if chunk.done:
                break
        await asyncio.create_task(stream.aclose())
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    _assert_clean(spans[0])
    assert "error.type" not in spans[0].attributes
    assert spans[0].attributes["gen_ai.usage.output_tokens"] == 3
    assert spans[0].attributes["ai4ia.gen_ai.usage.coverage"] == "known"


async def test_each_child_model_call_owns_its_tokens_and_parameters(capture):
    exporter, _ = capture
    model, deployment = _model()
    parent = ModelCallRecorder(model_id=model.id, deployment=deployment)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=_native("chat", model.id)),
    )) as http:
        gateway = _gateway(http)
        with parent.bind():
            for limit in (100, 200):
                child = ModelCallRecorder(model_id=model.id, deployment=deployment)
                await child.observe(gateway.complete(
                    deployment=deployment, messages=[], params={"max_tokens": limit},
                ))
                assert child.count == 1
    spans = exporter.get_finished_spans()
    assert parent.count == 0 and len(spans) == 2
    assert [span.attributes["gen_ai.request.max_tokens"] for span in spans] == [100, 200]
    assert sum(span.attributes["gen_ai.usage.output_tokens"] for span in spans) == 6


@pytest.mark.parametrize("enabled", [False, True])
async def test_per_app_gate_overrides_poisoned_content_environment(capture, enabled):
    exporter, _ = capture
    model, deployment = _model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=_native("chat", model.id)),
    )) as http:
        await _gateway(http, enabled=enabled).complete(deployment=deployment, messages=[])
    assert len(exporter.get_finished_spans()) == int(enabled)
