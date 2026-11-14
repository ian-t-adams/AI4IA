"""Synthetic shared-dispatch redirects, body isolation and legacy controls."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ai4ia_api import logging_setup
from ai4ia_api.gateway.attempts import no_replay_scope
from ai4ia_api.gateway.client import ModelGatewayClient, ModelGatewayError
from ai4ia_api.hard_quota.models import QuotaError
from tests.test_shared_gateway_chat_api import deny_network as deny_network
from tests.test_shared_gateway_staging import DEPLOYMENT, TOKEN, _client, _manifest, _settings, _token


class ObservedBody(httpx.AsyncByteStream):
    def __init__(self, content: bytes, *, error: BaseException | None = None):
        self.content = content
        self.error = error
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        self.reads += 1
        if self.error is not None:
            raise self.error
        yield self.content

    async def aclose(self):
        self.closed = True


def _body(api: str, streaming: bool) -> bytes:
    if api == "responses":
        data = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "synthetic answer"}],
                },
            ],
        }
        events = [
            {"type": "response.output_text.delta", "delta": "synthetic answer"},
            {"type": "response.completed", "response": data},
        ]
    else:
        data = {"choices": [{"message": {"content": "synthetic answer"}}]}
        events = [{"choices": [{"delta": {"content": "synthetic answer"}}]}]
    data["private"] = "PRIVATE redirect body"
    if streaming:
        return (
            "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            + "data: [DONE]\n\n"
        ).encode()
    return json.dumps(data).encode()


async def _dispatch(client: ModelGatewayClient, api: str, streaming: bool):
    kwargs = {
        "deployment": DEPLOYMENT,
        "messages": [{"role": "user", "content": "synthetic prompt"}],
        "api": api,
        "shared_gateway_eligible": True,
        "shared_gateway_sku": "GlobalStandard",
    }
    if streaming:
        chunks = [chunk async for chunk in client.stream(**kwargs)]
        assert "".join(chunk.delta for chunk in chunks if chunk.delta) == "synthetic answer"
        assert chunks[-1].done
    else:
        result = await client.complete(**kwargs)
        assert result["choices"][0]["message"]["content"] == "synthetic answer"


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("client_follows", [False, True])
@pytest.mark.parametrize("status", [300, 301, 302, 303, 304, 307, 308, 399])
@pytest.mark.parametrize(
    "location",
    [
        None,
        "https://gateway.example.invalid/unexpected?PRIVATE=target",
        "https://redirect.example.invalid/collect?PRIVATE=target",
    ],
)
async def test_shared_redirect_is_one_dispatch_without_reading_body(
    api, streaming, client_follows, status, location,
):
    requests = []
    bodies = []

    def handler(request):
        requests.append(request)
        body = ObservedBody(_body(api, streaming))
        bodies.append(body)
        return httpx.Response(
            status if len(requests) == 1 else 200,
            headers={"Location": location} if location else {},
            stream=body,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=client_follows,
    ) as http:
        with pytest.raises(ModelGatewayError) as error:
            await _dispatch(_client(http), api, streaming)
        assert not http.is_closed

    assert len(requests) == 1
    assert requests[0].url.host == "gateway.example.invalid"
    assert bodies[0].reads == 0
    assert bodies[0].closed
    assert error.value.status_code == 502
    assert error.value.shared_gateway
    assert error.value.retry_after_seconds is None
    assert error.value.detail == (
        "Model stream failed." if streaming else "Model gateway request failed."
    )
    for private in ("PRIVATE", "example.invalid", "synthetic answer"):
        assert private not in str(error.value)


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("client_follows", [False, True])
async def test_shared_success_reads_and_closes_response(api, streaming, client_follows):
    requests = []
    body = ObservedBody(_body(api, streaming))

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=client_follows,
    ) as http:
        await _dispatch(_client(http), api, streaming)
        assert not http.is_closed

    assert len(requests) == 1
    assert body.reads == 1
    assert body.closed


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_shared_read_failure_or_cancellation_closes_response(api, streaming, cancelled):
    requests = []
    body = ObservedBody(
        b"",
        error=asyncio.CancelledError() if cancelled else httpx.ReadError("PRIVATE transport"),
    )

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True,
    ) as http:
        with pytest.raises(asyncio.CancelledError if cancelled else ModelGatewayError) as error:
            await _dispatch(_client(http), api, streaming)
        assert not http.is_closed

    assert len(requests) == 1
    assert body.closed
    if not cancelled:
        assert error.value.status_code == 502
        assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_shared_redirect_telemetry_is_single_attempt_and_content_free(api, streaming, monkeypatch):
    provider = TracerProvider(resource=Resource({"service.name": "ai4ia-api"}))
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    monkeypatch.setattr(logging_setup, "_telemetry_configured", True)
    body = ObservedBody(_body(api, streaming))

    def handler(request):
        return httpx.Response(
            307, headers={"Location": "https://redirect.example.invalid/PRIVATE"}, stream=body,
        )

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True,
        ) as http:
            settings = _settings(manifest=_manifest()).model_copy(update={
                "applicationinsights_connection_string": "InstrumentationKey=synthetic",
            })
            client = ModelGatewayClient(
                settings, http_client=http, shared_gateway_token_provider=_token,
            )
            with pytest.raises(ModelGatewayError):
                await _dispatch(client, api, streaming)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.status.status_code == trace.StatusCode.ERROR
        assert span.attributes["ai4ia.gen_ai.http_attempts"] == 1
        assert span.attributes["ai4ia.gen_ai.usage.coverage"] == "unknown"
        assert span.attributes["error.type"] == "provider"
        assert not span.events
        assert span.status.description is None
        emitted = json.dumps(dict(span.attributes))
        for private in ("PRIVATE", "example.invalid", "synthetic prompt", "synthetic answer", TOKEN):
            assert private not in emitted
        assert body.reads == 0
        assert body.closed
    finally:
        provider.shutdown()


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_shared_still_refuses_no_replay_before_dispatch(api, streaming):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with no_replay_scope("synthetic-owner"), pytest.raises(QuotaError, match="no-replay"):
            await _dispatch(_client(http), api, streaming)

    assert requests == []


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_legacy_follow_enabled_behavior_is_unchanged(api, streaming):
    requests = []
    bodies = []

    def handler(request):
        requests.append(request)
        body = ObservedBody(_body(api, streaming))
        bodies.append(body)
        return httpx.Response(
            307 if len(requests) == 1 else 200,
            headers={"Location": "https://existing-gateway.example.invalid/final"},
            stream=body,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True,
    ) as http:
        client = ModelGatewayClient(
            _settings(enabled=False, manifest=_manifest()), http_client=http,
        )
        await _dispatch(client, api, streaming)
        assert not http.is_closed

    assert len(requests) == 2
    assert requests[1].url.path == "/final"
    assert all(body.closed for body in bodies)
