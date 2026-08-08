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
from ai4ia_api.usage.pricing import load_pricing
from tests.conftest import make_settings

_USAGE = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
# These tests assert the metering *pipeline* (usage is captured, costed, and
# aggregated), not any particular list price. Deriving the expectation from the
# packaged price book keeps that intent intact when rates are refreshed —
# hardcoding it made an unrelated pricing correction fail two tests here.
# Correctness of the rates themselves is covered by tests/test_usage_pricing.py.
_EXPECTED_MICRO_USD = load_pricing().estimate(
    "gpt-5.2",
    prompt_tokens=_USAGE["prompt_tokens"],
    completion_tokens=_USAGE["completion_tokens"],
).micro_usd
# Guard against the derivation going vacuous: if gpt-5.2 ever leaves the price
# book, estimate() returns None and the assertions below would stop proving
# anything. Fail at collection instead.
assert _EXPECTED_MICRO_USD is not None and _EXPECTED_MICRO_USD > 0


class UsageGateway:
    """Reports usage on both the non-stream result and the final stream chunk."""

    def __init__(self, text: str = "hello there") -> None:
        self.text = text

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        return {
            "choices": [{"message": {"role": "assistant", "content": self.text}}],
            "usage": dict(_USAGE),
        }

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        for piece in self.text.split():
            payload = {"choices": [{"delta": {"content": piece + " "}}]}
            yield ChatChunk(delta=piece + " ", raw=json.dumps(payload))
        # Final empty-choices usage chunk (as Azure emits with include_usage).
        yield ChatChunk(raw=json.dumps({"choices": [], "usage": dict(_USAGE)}), usage=dict(_USAGE))
        yield ChatChunk(done=True, raw="[DONE]")


class FailingStreamGateway:
    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        return {"choices": [{"message": {"content": ""}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        if False:  # pragma: no cover - generator that yields nothing before raising
            yield ChatChunk()
        raise ModelGatewayError(502, "upstream boom")


class FailingCompleteGateway:
    async def complete(self, **_kwargs):
        raise ModelGatewayError(502, "upstream echoed private prompt")


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


def test_non_stream_gateway_failure_persists_error_and_unknown_usage(usage_client):
    usage_client.app.state.gateway = FailingCompleteGateway()
    sid = _new_session(usage_client)

    response = usage_client.post(
        "/api/chat", json={"sessionId": sid, "content": "private prompt", "stream": False}
    )
    messages = usage_client.get(f"/api/sessions/{sid}/messages").json()
    summary = usage_client.get("/api/usage").json()

    assert response.status_code == 502
    assert response.json()["detail"] == "Chat completion failed."
    assert "upstream echoed" not in response.text
    assert [(message["role"], message["status"]) for message in messages] == [
        ("user", "complete"),
        ("assistant", "error"),
    ]
    assert messages[-1]["content"] == ""
    assert "upstream echoed" not in json.dumps(messages)
    assert summary["totalRequests"] == 1
    assert summary["erroredRequests"] == 1
    assert summary["unknownUsageRequests"] == 1
    assert summary["billableRequests"] == 0


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
