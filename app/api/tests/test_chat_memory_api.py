"""End-to-end memory wiring through POST /api/chat.

A real MemoryService (fake embedder + in-memory store) is attached to
``app.state.memory`` so we can assert: recalled memory is injected as an
untrusted system block, only USER text is remembered, recall is per-user
isolated, and ``/forget`` clears it.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ChatChunk
from ai4ia_api.main import create_app
from ai4ia_api.memory.in_memory import InMemoryVectorStore
from ai4ia_api.memory.service import MemoryService
from tests.conftest import make_settings


class CapturingGateway:
    """Records the messages of the most recent completion and replies fixed prose."""

    def __init__(self, reply: str = "Noted.") -> None:
        self.reply = reply
        self.last_messages: list[dict] | None = None
        self.calls = 0

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        self.last_messages = list(messages)
        return {"choices": [{"message": {"role": "assistant", "content": self.reply}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        self.last_messages = list(messages)
        yield ChatChunk(delta=self.reply, raw=json.dumps({"choices": [{"delta": {"content": self.reply}}]}))
        yield ChatChunk(done=True, raw="[DONE]")


class KeywordEmbedder:
    """Embeds by keyword overlap so 'favorite color' queries match the stored line.

    Dimensions are [color, clearance, other]; a text scores 1 on a dim if it
    contains that keyword, giving deterministic cosine similarity in tests.
    """

    async def embed(self, inputs):
        return [await self.embed_one(t) for t in inputs]

    async def embed_one(self, text: str) -> list[float]:
        t = text.lower()
        color = 1.0 if "color" in t or "colour" in t else 0.0
        clearance = 1.0 if "clearance" in t else 0.0
        other = 0.0 if (color or clearance) else 1.0
        return [color, clearance, other]


def _memory_service() -> MemoryService:
    return MemoryService(
        store=InMemoryVectorStore(),
        embedder=KeywordEmbedder(),
        min_score=0.5,
        top_k=5,
        min_chars_to_store=8,
    )


@pytest.fixture
def mem_client():
    app = create_app(make_settings())
    with TestClient(app) as c:
        c.app.state.gateway = CapturingGateway()
        c.app.state.memory = _memory_service()
        yield c


def _new_session(client, headers=None) -> str:
    resp = client.post("/api/sessions", json={"title": "Chat", "model": "gpt-5.2"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_recall_injects_untrusted_block_with_prior_user_text(mem_client):
    gw = mem_client.app.state.gateway
    sid = _new_session(mem_client)

    # Turn 1: a durable user statement (gets remembered).
    r1 = mem_client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "My favorite color is orange", "stream": False},
    )
    assert r1.status_code == 200, r1.text

    # Turn 2: a related question -> the prior statement should be recalled.
    r2 = mem_client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "What is my favorite color?", "stream": False},
    )
    assert r2.status_code == 200, r2.text

    msgs = gw.last_messages
    system_texts = [m["content"] for m in msgs if m["role"] == "system"]
    joined = "\n".join(system_texts)
    assert "My favorite color is orange" in joined
    assert "UNTRUSTED" in joined
    # The assistant's prior reply must NOT be remembered (user-only memory).
    assert gw.reply not in joined


def test_recall_is_user_isolated(mem_client):
    alice = {"X-Dev-User": "alice"}
    bob = {"X-Dev-User": "bob"}
    gw = mem_client.app.state.gateway

    sid_a = _new_session(mem_client, headers=alice)
    mem_client.post(
        "/api/chat",
        json={"sessionId": sid_a, "content": "The clearance code is alpha-seven", "stream": False},
        headers=alice,
    )

    sid_b = _new_session(mem_client, headers=bob)
    r = mem_client.post(
        "/api/chat",
        json={"sessionId": sid_b, "content": "What is the clearance code?", "stream": False},
        headers=bob,
    )
    assert r.status_code == 200, r.text

    joined = "\n".join(m["content"] for m in gw.last_messages if m["role"] == "system")
    assert "alpha-seven" not in joined


def test_forget_session_clears_recall(mem_client):
    gw = mem_client.app.state.gateway
    sid = _new_session(mem_client)

    mem_client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "My favorite color is orange", "stream": False},
    )

    forget = mem_client.post(
        "/api/chat", json={"sessionId": sid, "content": "/forget", "stream": False}
    )
    assert forget.status_code == 200, forget.text
    assert "Forgot 1" in forget.json()["message"]["content"]

    # A later related question recalls nothing now.
    mem_client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "What is my favorite color?", "stream": False},
    )
    joined = "\n".join(m["content"] for m in gw.last_messages if m["role"] == "system")
    assert "My favorite color is orange" not in joined


def test_optional_memory_context_is_dropped_before_it_can_overflow_model_budget(
    mem_client,
):
    class OversizedMemory:
        async def recall(self, *_args):
            return []

        def format_context(self, _recalled):
            return "UNTRUSTED MEMORY\n" + ("x" * 5000)

        async def remember(self, *_args):
            return None

        async def close(self):
            return None

    entry = mem_client.app.state.catalog.get("gpt-5.2")
    assert entry is not None
    entry.contextWindow = 7000
    entry.maxOutputTokens = 256
    mem_client.app.state.memory = OversizedMemory()
    gateway = mem_client.app.state.gateway
    sid = _new_session(mem_client)

    response = mem_client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "fit this turn", "stream": False},
    )

    assert response.status_code == 200, response.text
    assert gateway.calls == 1
    assert gateway.last_messages is not None
    assert all(
        "UNTRUSTED MEMORY" not in message["content"]
        for message in gateway.last_messages
    )


def test_combined_fixed_prompt_overflow_fails_before_gateway_call(mem_client):
    entry = mem_client.app.state.catalog.get("gpt-5.2")
    assert entry is not None
    entry.contextWindow = 7000
    entry.maxOutputTokens = 256
    gateway = mem_client.app.state.gateway
    created = mem_client.post(
        "/api/sessions",
        json={
            "title": "bounded",
            "model": "gpt-5.2",
            "systemPrompt": "s" * 1800,
        },
    )
    assert created.status_code == 201, created.text

    response = mem_client.post(
        "/api/chat",
        json={
            "sessionId": created.json()["id"],
            "content": "u" * 800,
            "stream": False,
        },
    )

    assert response.status_code == 422
    assert "do not fit" in response.json()["detail"]
    assert gateway.calls == 0
