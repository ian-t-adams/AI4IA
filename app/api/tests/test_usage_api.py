"""End-to-end usage metering through POST /api/chat and GET /api/usage.

A gateway that reports token usage is attached so we can assert that completed
turns are metered (non-stream + stream), that cancelled/errored turns are
recorded as non-billable, and that the summary endpoint is per-user scoped.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ChatChunk, ModelGatewayError
from ai4ia_api.main import create_app
from tests.conftest import make_settings

_USAGE = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
# gpt-5.2 packaged price: 1000*1.25 + 500*10.0 = 1250 + 5000 = 6250 micro-USD.
_EXPECTED_MICRO_USD = 6250


class UsageGateway:
    """Reports usage on both the non-stream result and the final stream chunk."""

    def __init__(self, text: str = "hello there") -> None:
        self.text = text

    async def complete(self, *, deployment, messages, params=None, correlation_id=None):
        return {
            "choices": [{"message": {"role": "assistant", "content": self.text}}],
            "usage": dict(_USAGE),
        }

    async def stream(self, *, deployment, messages, params=None, correlation_id=None):
        for piece in self.text.split():
            payload = {"choices": [{"delta": {"content": piece + " "}}]}
            yield ChatChunk(delta=piece + " ", raw=json.dumps(payload))
        # Final empty-choices usage chunk (as Azure emits with include_usage).
        yield ChatChunk(raw=json.dumps({"choices": [], "usage": dict(_USAGE)}), usage=dict(_USAGE))
        yield ChatChunk(done=True, raw="[DONE]")


class FailingStreamGateway:
    async def complete(self, *, deployment, messages, params=None, correlation_id=None):
        return {"choices": [{"message": {"content": ""}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None):
        if False:  # pragma: no cover - generator that yields nothing before raising
            yield ChatChunk()
        raise ModelGatewayError(502, "upstream boom")


@pytest.fixture
def usage_client():
    app = create_app(make_settings())
    with TestClient(app) as c:
        c.app.state.gateway = UsageGateway()
        yield c


def _new_session(client, headers=None) -> str:
    resp = client.post(
        "/api/sessions", json={"title": "Chat", "model": "gpt-5.2"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_non_stream_turn_is_metered(usage_client):
    sid = _new_session(usage_client)
    r = usage_client.post(
        "/api/chat", json={"sessionId": sid, "content": "hi", "stream": False}
    )
    assert r.status_code == 200, r.text

    summary = usage_client.get("/api/usage").json()
    assert summary["totalRequests"] == 1
    assert summary["billableRequests"] == 1
    assert summary["totalTokens"] == 1500
    assert summary["totalCostMicroUsd"] == _EXPECTED_MICRO_USD
    assert summary["byModel"][0]["model"] == "gpt-5.2"


def test_streaming_turn_captures_usage(usage_client):
    sid = _new_session(usage_client)
    with usage_client.stream(
        "POST", "/api/chat", json={"sessionId": sid, "content": "hi", "stream": True}
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "[DONE]" in body

    summary = usage_client.get("/api/usage").json()
    assert summary["totalRequests"] == 1
    assert summary["billableRequests"] == 1
    assert summary["totalTokens"] == 1500
    assert summary["totalCostMicroUsd"] == _EXPECTED_MICRO_USD


def test_errored_stream_records_non_billable(usage_client):
    usage_client.app.state.gateway = FailingStreamGateway()
    sid = _new_session(usage_client)
    with usage_client.stream(
        "POST", "/api/chat", json={"sessionId": sid, "content": "hi", "stream": True}
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "error" in body

    summary = usage_client.get("/api/usage").json()
    assert summary["totalRequests"] == 1
    assert summary["erroredRequests"] == 1
    assert summary["billableRequests"] == 0
    assert summary["totalCostMicroUsd"] == 0


def test_usage_is_user_isolated(usage_client):
    alice = {"X-Dev-User": "alice"}
    bob = {"X-Dev-User": "bob"}
    sid_a = _new_session(usage_client, headers=alice)
    usage_client.post(
        "/api/chat",
        json={"sessionId": sid_a, "content": "hi", "stream": False},
        headers=alice,
    )

    bob_summary = usage_client.get("/api/usage", headers=bob).json()
    assert bob_summary["totalRequests"] == 0
    alice_summary = usage_client.get("/api/usage", headers=alice).json()
    assert alice_summary["totalRequests"] == 1


def test_usage_since_days_is_bounded(usage_client):
    assert usage_client.get("/api/usage?since_days=0").status_code == 422
    assert usage_client.get("/api/usage?since_days=91").status_code == 422
    assert usage_client.get("/api/usage?since_days=30").status_code == 200
