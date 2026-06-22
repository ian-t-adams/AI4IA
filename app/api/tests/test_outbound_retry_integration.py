"""Integration tests proving the transient-retry policy is wired into our
bespoke outbound httpx clients (model gateway + Content Understanding) at the
idempotent GET call sites only, and is NOT applied to writes.

The upstream is faked with ``httpx.MockTransport``. Transient (503) responses
carry ``Retry-After: 0`` so the real backoff sleeps instantly and
deterministically while still exercising the end-to-end retry path.
"""
from __future__ import annotations

import httpx
import pytest

from ai4ia_api.content_understanding.client import (
    ContentUnderstandingClient,
    ContentUnderstandingError,
)
from ai4ia_api.gateway.client import ModelGatewayClient, ModelGatewayError
from tests.conftest import make_settings

_RETRY = {"outbound_retry_max_attempts": 3, "outbound_retry_deadline_seconds": 20.0}


def _gateway(handler, **overrides) -> ModelGatewayClient:
    settings = make_settings(
        model_gateway_url="http://gw.test/openai", **_RETRY, **overrides
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ModelGatewayClient(settings, http_client=http)


@pytest.mark.asyncio
async def test_get_video_job_retries_transient_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "0"}, text="busy")
        return httpx.Response(200, json={"id": "job-1", "status": "succeeded"})

    client = _gateway(handler)
    job = await client.get_video_job(job_id="job-1")
    assert job["status"] == "succeeded"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_get_video_job_does_not_retry_404():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="missing")

    client = _gateway(handler)
    with pytest.raises(ModelGatewayError) as excinfo:
        await client.get_video_job(job_id="nope")
    assert excinfo.value.status_code == 404
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_get_video_content_retries_transient_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, content=b"MP4BYTES")

    client = _gateway(handler)
    content = await client.get_video_content(generation_id="gen-1")
    assert content == b"MP4BYTES"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_chat_completion_post_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, headers={"Retry-After": "0"}, text="busy")

    client = _gateway(handler)
    with pytest.raises(ModelGatewayError) as excinfo:
        await client.complete(deployment="gpt-4.1", messages=[{"role": "user", "content": "hi"}])
    assert excinfo.value.status_code == 503
    assert calls["n"] == 1  # a write must run exactly once


def _cu_settings(**overrides):
    return make_settings(
        cu_base_url="https://cu.example",
        cu_auth_mode="api_key",
        cu_api_key="secret-key",
        cu_poll_interval_seconds=0.0,
        cu_max_poll_seconds=5.0,
        **_RETRY,
        **overrides,
    )


async def _noop_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_cu_poll_retries_transient_then_succeeds():
    op_url = "https://cu.example/contentunderstanding/analyzerResults/req-1?api-version=2025-11-01"
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, headers={"operation-location": op_url})
        state["polls"] += 1
        if state["polls"] == 1:
            return httpx.Response(503, headers={"Retry-After": "0"}, text="busy")
        return httpx.Response(
            200,
            json={
                "id": "req-1",
                "status": "Succeeded",
                "result": {"analyzerId": "prebuilt-documentSearch"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(_cu_settings(), http_client=http)
        result = await client.analyze(
            "prebuilt-documentSearch", b"x", "application/pdf", sleep=_noop_sleep
        )
    assert result.analyzer_id == "prebuilt-documentSearch"
    assert state["polls"] == 2  # one transient 503, then the succeeded poll


@pytest.mark.asyncio
async def test_cu_poll_does_not_retry_400():
    op_url = "https://cu.example/contentunderstanding/analyzerResults/req-1?api-version=2025-11-01"
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, headers={"operation-location": op_url})
        state["polls"] += 1
        return httpx.Response(400, text="bad request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(_cu_settings(), http_client=http)
        with pytest.raises(ContentUnderstandingError) as excinfo:
            await client.analyze("a", b"x", "application/pdf", sleep=_noop_sleep)
    assert excinfo.value.status_code == 400
    assert state["polls"] == 1  # non-transient 4xx is not retried
