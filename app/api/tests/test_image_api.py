"""Image-generation endpoint contract + governance.

Covers the happy path, input validation, catalog/category rejection, entitlement
gating (disabled + admin rate limit), upstream-error sanitization, and the core
governance guarantee that a successful image request is metered into the usage
ledger (so rolling rate/budget windows account for image traffic, not just chat).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.main import create_app
from tests.conftest import make_settings

ADMIN = {"X-Dev-User": "alice"}
TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


class FakeImageGateway:
    """Stand-in gateway for image tests: returns canned data or raises."""

    def __init__(self) -> None:
        self.error: ModelGatewayError | None = None
        self.usage: dict | None = {"input_tokens": 10, "output_tokens": 100, "total_tokens": 110}
        self.calls: list[dict] = []

    async def generate_image(self, *, deployment, prompt, size=None, n=1, extra=None, correlation_id=None):
        self.calls.append({"deployment": deployment, "prompt": prompt, "size": size, "n": n})
        if self.error is not None:
            raise self.error
        return {"data": [{"b64_json": TINY_PNG_B64} for _ in range(n)], "usage": self.usage}


def _client(**settings_overrides) -> TestClient:
    app = create_app(make_settings(admin_subjects="alice", **settings_overrides))
    c = TestClient(app)
    c.__enter__()
    c.app.state.gateway = FakeImageGateway()
    return c


@pytest.fixture
def client():
    c = _client()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _internal_id(client, headers) -> str:
    return client.get("/api/entitlement", headers=headers).json()["userId"]


# ---- happy path ----


def test_generate_success(client):
    r = client.post(
        "/api/images/generations",
        json={"prompt": "an orange cyberpunk skyline", "model": "gpt-image-2"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "gpt-image-2"
    assert body["deployment"].startswith("gpt-image-2")
    assert body["size"] == "1024x1024"
    assert len(body["images"]) == 1
    assert body["images"][0]["b64"] == TINY_PNG_B64


def test_default_model_used_when_omitted(client):
    r = client.post(
        "/api/images/generations",
        json={"prompt": "a logo"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 200, r.text
    # The default is the first image-category model in the catalog.
    assert r.json()["model"] in {"gpt-image-1.5", "gpt-image-2", "MAI-Image-2.5"}


def test_size_passthrough(client):
    r = client.post(
        "/api/images/generations",
        json={"prompt": "x", "model": "gpt-image-2", "size": "1536x1024"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["size"] == "1536x1024"
    assert client.app.state.gateway.calls[-1]["size"] == "1536x1024"


def test_auto_size_sends_none_to_gateway(client):
    r = client.post(
        "/api/images/generations",
        json={"prompt": "x", "model": "gpt-image-2", "size": "auto"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 200, r.text
    assert client.app.state.gateway.calls[-1]["size"] is None


# ---- validation ----


def test_empty_prompt_rejected(client):
    r = client.post(
        "/api/images/generations",
        json={"prompt": "   ", "model": "gpt-image-2"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 422


def test_n_above_cap_rejected(client):
    r = client.post(
        "/api/images/generations",
        json={"prompt": "x", "model": "gpt-image-2", "n": 2},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 422


def test_bad_size_rejected(client):
    r = client.post(
        "/api/images/generations",
        json={"prompt": "x", "model": "gpt-image-2", "size": "999x999"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 422


def test_unknown_model_rejected(client):
    r = client.post(
        "/api/images/generations",
        json={"prompt": "x", "model": "no-such-model"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 400


def test_non_image_model_rejected(client):
    r = client.post(
        "/api/images/generations",
        json={"prompt": "x", "model": "gpt-5.2"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 400
    assert "not an image model" in r.json()["detail"]


# ---- entitlement gating ----


def test_disabled_user_forbidden(client):
    headers = {"X-Dev-User": "banned"}
    uid = _internal_id(client, headers)
    client.put(f"/api/admin/entitlements/{uid}", json={"disabled": True}, headers=ADMIN)
    r = client.post(
        "/api/images/generations",
        json={"prompt": "x", "model": "gpt-image-2"},
        headers=headers,
    )
    assert r.status_code == 403


def test_rate_limited_user_gets_429_with_retry_after(client):
    headers = {"X-Dev-User": "capped"}
    uid = _internal_id(client, headers)
    client.put(
        f"/api/admin/entitlements/{uid}", json={"requestsPerMinute": 0}, headers=ADMIN
    )
    r = client.post(
        "/api/images/generations",
        json={"prompt": "x", "model": "gpt-image-2"},
        headers=headers,
    )
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "60"


# ---- upstream error sanitization ----


def test_upstream_content_policy_400_surfaced(client):
    client.app.state.gateway.error = ModelGatewayError(400, "content_policy_violation: blocked")
    r = client.post(
        "/api/images/generations",
        json={"prompt": "x", "model": "gpt-image-2"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 400
    assert "content_policy" in r.json()["detail"]


def test_upstream_500_mapped_to_generic_502(client):
    client.app.state.gateway.error = ModelGatewayError(500, "internal stack trace leak")
    r = client.post(
        "/api/images/generations",
        json={"prompt": "x", "model": "gpt-image-2"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "Image generation failed."
    assert "stack trace" not in r.json()["detail"]


# ---- governance: image requests are metered ----


def test_successful_image_is_metered(client):
    headers = {"X-Dev-User": "meterme"}
    client.post(
        "/api/images/generations",
        json={"prompt": "x", "model": "gpt-image-2"},
        headers=headers,
    )
    summary = client.get("/api/usage", headers=headers).json()
    assert summary["totalRequests"] >= 1
    # Token usage from the image response is captured (input+output mapped).
    assert summary["totalTokens"] >= 110
