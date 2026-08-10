"""The chat request's generation-parameter allowlist.

``ChatRequest.params`` used to be an open ``dict`` that was merged straight into
the provider request body. FastAPI is the trust boundary and direct API callers
are supported, so that let a caller smuggle arbitrary fields upstream:

* ``messages``/``input`` -- replace the server-built history (and its system
  prompt) with an attacker-chosen conversation;
* ``model`` -- re-target a different deployment, past catalog routing, region
  and data-zone selection;
* ``store`` -- switch provider-side retention back on for a turn;
* ``tools`` -- call provider-side tools outside the governed tool registry.

These tests pin the allowlist itself. The independent second layer (the gateway
builders stripping and rewriting server-owned fields) is covered in
``test_gateway_client.py``.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai4ia_api.routers.chat import MAX_CHAT_CONTENT_CHARS, ChatParams, ChatRequest

# Every field a caller has ever been able to smuggle through to the provider.
FORBIDDEN = [
    "messages",
    "input",
    "model",
    "instructions",
    "store",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "stream",
    "stream_options",
    "previous_response_id",
    "max_completion_tokens",
]


@pytest.mark.parametrize("key", FORBIDDEN)
def test_reserved_and_unknown_params_are_rejected(key):
    with pytest.raises(ValidationError):
        ChatParams.model_validate({key: "whatever"})


def test_supported_generation_controls_round_trip():
    params = ChatParams.model_validate(
        {"temperature": 0.5, "top_p": 0.9, "max_tokens": 256, "reasoning_effort": "high"}
    )
    assert params.model_dump(exclude_none=True) == {
        "temperature": 0.5,
        "top_p": 0.9,
        "max_tokens": 256,
        "reasoning_effort": "high",
    }


def test_unset_params_dump_empty_so_server_defaults_apply():
    """An omitted control must not become an explicit null in the request body:
    the per-model ceiling logic keys off the absence of ``max_tokens``."""
    assert ChatParams().model_dump(exclude_none=True) == {}
    assert ChatRequest(sessionId="s", content="hi").params.model_dump(
        exclude_none=True
    ) == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"temperature": 2.1},
        {"temperature": -0.1},
        {"top_p": 1.5},
        {"top_p": -1},
        {"max_tokens": 0},
        {"max_tokens": -5},
        {"reasoning_effort": "x" * 33},
    ],
)
def test_out_of_range_values_are_rejected(payload):
    with pytest.raises(ValidationError):
        ChatParams.model_validate(payload)


@pytest.mark.parametrize("key", ["messages", "model", "store", "tools"])
def test_api_rejects_reserved_params_with_422(client, key):
    """Enforced at the HTTP boundary, before the handler or any gateway call."""
    response = client.post(
        "/api/chat",
        json={
            "sessionId": "does-not-need-to-exist",
            "content": "hi",
            "params": {key: [{"role": "system", "content": "injected"}]},
        },
    )
    assert response.status_code == 422


def test_chat_content_accepts_exact_boundary_and_rejects_one_character_over():
    assert MAX_CHAT_CONTENT_CHARS == 32_000
    accepted = ChatRequest(sessionId="s", content="x" * 32_000)
    assert len(accepted.content) == 32_000
    with pytest.raises(ValidationError):
        ChatRequest(sessionId="s", content="x" * 32_001)
