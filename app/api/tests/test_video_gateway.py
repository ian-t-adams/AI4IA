"""URL/body shaping + status-code handling for the Sora video gateway methods.

Uses a tiny fake httpx client (no network) to capture the exact request the
gateway emits for create/poll/download, mirroring the image gateway tests.
"""
from __future__ import annotations

import pytest

from ai4ia_api.config import GatewayProviderStyle
from ai4ia_api.gateway.client import ModelGatewayClient, ModelGatewayError
from tests.conftest import make_settings


class _Resp:
    def __init__(self, status_code=200, json_body=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.content = content
        self.text = text

    def json(self):
        return self._json


class FakeHttp:
    """Records POST/GET calls and returns canned responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return self._responses.pop(0)

    async def get(self, url, headers=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return self._responses.pop(0)


def _client(responses, **overrides) -> tuple[ModelGatewayClient, FakeHttp]:
    http = FakeHttp(responses)
    return ModelGatewayClient(make_settings(**overrides), http_client=http), http


async def test_create_video_job_url_and_body():
    c, http = _client([_Resp(json_body={"id": "job1", "status": "queued"})])
    out = await c.create_video_job(
        deployment="sora-2-dep", prompt="a cat", width=1280, height=720, n_seconds=4
    )
    assert out == {"id": "job1", "status": "queued"}
    call = http.calls[0]
    assert call["url"] == (
        "http://gateway.test/deployments/sora-2-dep/videos?api-version=preview"
    )
    assert call["json"]["model"] == "sora-2-dep"
    assert call["json"]["size"] == "1280x720"
    assert call["json"]["seconds"] == "4"


async def test_get_video_job_url():
    c, http = _client([_Resp(json_body={"status": "completed"})])
    out = await c.get_video_job(deployment="sora-2-dep", job_id="job1")
    assert out["status"] == "completed"
    assert http.calls[0]["url"] == (
        "http://gateway.test/deployments/sora-2-dep/videos/job1?api-version=preview"
    )


async def test_get_video_content_returns_bytes():
    c, http = _client([_Resp(content=b"MP4BYTES")])
    out = await c.get_video_content(deployment="sora-2-dep", video_id="video1")
    assert out == b"MP4BYTES"
    assert http.calls[0]["url"] == (
        "http://gateway.test/deployments/sora-2-dep/videos/video1/content"
        "?api-version=preview"
    )


async def test_video_uses_video_api_version():
    c, http = _client(
        [_Resp(json_body={"id": "j"})], gateway_video_api_version="2099-preview"
    )
    await c.create_video_job(
        deployment="d", prompt="x", width=720, height=1280, n_seconds=4
    )
    assert "api-version=2099-preview" in http.calls[0]["url"]


async def test_openai_compatible_video_omits_api_version():
    c, http = _client(
        [_Resp(json_body={"id": "j"})],
        gateway_provider_style=GatewayProviderStyle.openai_compatible,
    )
    await c.create_video_job(
        deployment="d", prompt="x", width=720, height=1280, n_seconds=4
    )
    assert http.calls[0]["url"] == "http://gateway.test/videos"
    assert "api-version" not in http.calls[0]["url"]


async def test_create_video_job_raises_on_error_status():
    c, _ = _client([_Resp(status_code=400, text="bad prompt")])
    with pytest.raises(ModelGatewayError) as ei:
        await c.create_video_job(
            deployment="d", prompt="x", width=720, height=1280, n_seconds=4
        )
    assert ei.value.status_code == 400
