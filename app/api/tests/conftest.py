"""Shared test fixtures."""
from __future__ import annotations

import json
from typing import Awaitable, Callable

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.config import Settings
from ai4ia_api.gateway.client import ChatChunk
from ai4ia_api.main import create_app


def make_settings(**overrides) -> Settings:
    base = dict(
        env="local",
        auth_provider="dev",
        allow_dev_auth=True,
        session_store="memory",
        model_gateway_url="http://gateway.test",
    )
    base.update(overrides)
    # _env_file=None isolates tests from a developer's local .env file.
    return Settings(_env_file=None, **base)


class FakeUsage:
    """Minimal usage-service stub shared by doc-ingest test modules."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def record_completion(self, **kwargs) -> None:
        self.calls.append(kwargs)


class FakeEmbedder:
    """Minimal embedder stub shared by doc-ingest test modules.

    Supports an optional ``on_embed`` async callback (used by the race tests for
    precise timing control) and a ``calls`` counter alongside the ``embedded``
    text list.  The output shape is always a 3-element float vector so that the
    in-memory chunk store (``expected_dim=3``) accepts it without adjustment.
    """

    def __init__(
        self,
        *,
        on_embed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.on_embed = on_embed
        self.embedded: list[str] = []
        self.calls: int = 0

    async def embed(self, inputs: list[str]) -> list[list[float]]:
        self.calls += 1
        self.embedded.extend(inputs)
        if self.on_embed is not None:
            await self.on_embed()
        return [[float(len(t) % 5), 1.0, 0.0] for t in inputs]


class FakeGateway:
    """Stand-in for ModelGatewayClient used in API flow tests."""

    def __init__(self, text: str = "hello world") -> None:
        self.text = text

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        return {"choices": [{"message": {"role": "assistant", "content": self.text}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        for piece in self.text.split():
            payload = {"choices": [{"delta": {"content": piece + " "}}]}
            yield ChatChunk(delta=piece + " ", raw=json.dumps(payload))
        yield ChatChunk(done=True, raw="[DONE]")


def _split(text: str, parts: int) -> list[str]:
    """Cut ``text`` into roughly ``parts`` pieces at arbitrary offsets."""
    if not text:
        return []
    size = max(1, -(-len(text) // parts))
    return [text[i : i + size] for i in range(0, len(text), size)]


def sse_chunks(response: dict, *, parts: int = 3) -> list[ChatChunk]:
    """Replay a non-streaming chat-completions response as the SSE a gateway emits.

    Used by the test gateways so a tool turn exercises the *streamed* wire shape
    rather than a shape only this repo produces. Two properties are deliberate:

    * assistant content is delivered as several deltas, so a test can prove text
      reached the client before a tool ran rather than after the turn finished;
    * every tool call's ``arguments`` is cut at arbitrary offsets — routinely
      mid-token, mid-key or mid-escape — because that is what a real provider
      does and it is the case a naive accumulator gets wrong. ``id``/``type``/
      ``name`` ride only on the first fragment, and the ``index`` (not array
      position) is what ties the rest of them back to it.
    """
    message = ((response.get("choices") or [{}])[0]).get("message") or {}
    out: list[ChatChunk] = []
    for piece in _split(message.get("content") or "", parts):
        out.append(
            ChatChunk(
                delta=piece,
                raw=json.dumps({"choices": [{"delta": {"content": piece}}]}),
            )
        )
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        head = {
            "index": index,
            "id": call.get("id"),
            "type": call.get("type", "function"),
            "function": {"name": function.get("name"), "arguments": ""},
        }
        out.append(ChatChunk(raw=json.dumps({"choices": [{"delta": {"tool_calls": [head]}}]})))
        for piece in _split(function.get("arguments") or "", parts):
            fragment = {"index": index, "function": {"arguments": piece}}
            out.append(
                ChatChunk(raw=json.dumps({"choices": [{"delta": {"tool_calls": [fragment]}}]}))
            )
    if response.get("usage"):
        out.append(
            ChatChunk(
                usage=response["usage"],
                raw=json.dumps({"choices": [], "usage": response["usage"]}),
            )
        )
    out.append(ChatChunk(done=True, raw="[DONE]"))
    return out


async def stream_like_gateway(response: dict, *, parts: int = 3):
    """Async-generator form of :func:`sse_chunks`, for a double's ``stream``."""
    for chunk in sse_chunks(response, parts=parts):
        yield chunk


@pytest.fixture
def client():
    app = create_app(make_settings())
    with TestClient(app) as c:
        c.app.state.gateway = FakeGateway()
        yield c
