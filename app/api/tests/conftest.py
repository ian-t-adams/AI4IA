"""Shared test fixtures."""
from __future__ import annotations

import json

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


class FakeGateway:
    """Stand-in for ModelGatewayClient used in API flow tests."""

    def __init__(self, text: str = "hello world") -> None:
        self.text = text

    async def complete(self, *, deployment, messages, params=None, correlation_id=None):
        return {"choices": [{"message": {"role": "assistant", "content": self.text}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None):
        for piece in self.text.split():
            payload = {"choices": [{"delta": {"content": piece + " "}}]}
            yield ChatChunk(delta=piece + " ", raw=json.dumps(payload))
        yield ChatChunk(done=True, raw="[DONE]")


@pytest.fixture
def client():
    app = create_app(make_settings())
    with TestClient(app) as c:
        c.app.state.gateway = FakeGateway()
        yield c
