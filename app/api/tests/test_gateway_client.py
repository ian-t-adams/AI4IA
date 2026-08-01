import httpx
import json
import pytest

from ai4ia_api.gateway.client import (
    ModelGatewayClient,
    ModelGatewayError,
    _messages_to_responses_input,
    _normalize_params_for_responses,
    _parse_responses_event,
    _responses_json_to_chat,
    parse_sse_line,
)
from tests.conftest import make_settings


def _client(transport=None, **settings_overrides):
    settings = make_settings(model_gateway_url="http://gw.test/openai", **settings_overrides)
    http = httpx.AsyncClient(transport=transport) if transport else None
    return ModelGatewayClient(settings, http_client=http)


def test_native_url_has_deployment_path_and_api_version():
    client = _client(gateway_provider_style="azure_openai_native", gateway_api_version="2024-10-21")
    req = client.build_request(deployment="gpt-5.2-slurmfactory-eastus2-glbl", messages=[])
    assert req.url == (
        "http://gw.test/openai/deployments/gpt-5.2-slurmfactory-eastus2-glbl"
        "/chat/completions?api-version=2024-10-21"
    )
    assert "model" not in req.json


def test_openai_compatible_url_and_model_in_body():
    client = _client(gateway_provider_style="openai_compatible")
    req = client.build_request(deployment="dep-1", messages=[{"role": "user", "content": "hi"}])
    assert req.url == "http://gw.test/openai/chat/completions"
    assert req.json["model"] == "dep-1"


def test_no_double_openai_prefix_with_custom_path():
    client = _client(gateway_chat_path="/deployments/{deployment}/chat/completions")
    req = client.build_request(deployment="dep-1", messages=[])
    assert "/openai/openai/" not in req.url


def test_correlation_and_api_key_headers():
    client = _client(model_gateway_auth_mode="api_key", model_gateway_api_key="secret")
    req = client.build_request(deployment="dep-1", messages=[], correlation_id="abc123")
    assert req.headers["x-correlation-id"] == "abc123"
    assert req.headers["Ocp-Apim-Subscription-Key"] == "secret"


def test_custom_api_key_header_separates_proxy_and_apim_credentials():
    client = _client(
        model_gateway_auth_mode="api_key",
        model_gateway_api_key="secret",
        model_gateway_api_key_header="S7P-KEY",
    )
    req = client.build_request(deployment="dep-1", messages=[])
    assert req.headers["S7P-KEY"] == "secret"
    assert "Ocp-Apim-Subscription-Key" not in req.headers


def test_stream_flag_added_only_when_streaming():
    client = _client()
    assert "stream" not in client.build_request(deployment="d", messages=[]).json
    assert client.build_request(deployment="d", messages=[], stream=True).json["stream"] is True


def test_include_usage_sets_stream_options_only_when_streaming():
    client = _client()
    # Non-streaming never sets stream_options even if include_usage is passed.
    plain = client.build_request(deployment="d", messages=[], include_usage=True)
    assert "stream_options" not in plain.json
    # Streaming + include_usage opts in.
    streamed = client.build_request(
        deployment="d", messages=[], stream=True, include_usage=True
    )
    assert streamed.json["stream_options"] == {"include_usage": True}
    # Streaming without include_usage must not carry it.
    off = client.build_request(deployment="d", messages=[], stream=True, include_usage=False)
    assert "stream_options" not in off.json


def test_parse_sse_line_captures_usage_chunk():
    # The final usage chunk has empty choices and a populated usage object.
    line = 'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}'
    chunk = parse_sse_line(line)
    assert chunk is not None
    assert chunk.delta == ""
    assert chunk.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


async def test_stream_retries_without_stream_options_on_400():
    attempts: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        had_options = "stream_options" in body
        attempts.append(had_options)
        if had_options:
            # Simulate a deployment that rejects stream_options with a 400.
            return httpx.Response(400, text="unknown parameter: stream_options")
        sse = (
            'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=sse)

    client = _client(transport=httpx.MockTransport(handler), gateway_stream_include_usage=True)
    chunks = [c async for c in client.stream(deployment="dep-1", messages=[])]
    # First attempt (with options) 400s, second (without) succeeds.
    assert attempts == [True, False]
    assert any(c.delta == "hi" for c in chunks)
    assert chunks[-1].done is True


async def test_stream_does_not_retry_on_non_400():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="rate limited")

    client = _client(transport=httpx.MockTransport(handler), gateway_stream_include_usage=True)
    with pytest.raises(ModelGatewayError) as exc:
        [c async for c in client.stream(deployment="dep-1", messages=[])]
    assert exc.value.status_code == 429
    # 429 is not a parameter problem: must fail immediately, no fallback attempt.
    assert calls["n"] == 1


@pytest.mark.parametrize(
    "line,expected_delta,expected_done",
    [
        ('data: {"choices":[{"delta":{"content":"hi"}}]}', "hi", False),
        ("data: [DONE]", "", True),
        (": comment", "", False),
        ("", "", False),
    ],
)
def test_parse_sse_line(line, expected_delta, expected_done):
    chunk = parse_sse_line(line)
    if expected_delta == "" and not expected_done and not line.startswith("data:"):
        assert chunk is None
        return
    assert chunk is not None
    assert chunk.delta == expected_delta
    assert chunk.done == expected_done


async def test_complete_posts_and_parses_json():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _client(transport=httpx.MockTransport(handler))
    result = await client.complete(deployment="dep-1", messages=[{"role": "user", "content": "x"}])
    assert result["choices"][0]["message"]["content"] == "ok"
    assert "deployments/dep-1/chat/completions" in captured["url"]


async def test_complete_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    client = _client(transport=httpx.MockTransport(handler))
    with pytest.raises(ModelGatewayError) as exc:
        await client.complete(deployment="dep-1", messages=[])
    assert exc.value.status_code == 429


def test_embed_request_native_url_path_and_body():
    client = _client(
        gateway_provider_style="azure_openai_native", gateway_api_version="2024-10-21"
    )
    req = client.build_embed_request(
        deployment="text-embedding-3-large-slurmfactory-eastus2-glbl",
        inputs=["a", "b"],
    )
    assert req.url == (
        "http://gw.test/openai/deployments/"
        "text-embedding-3-large-slurmfactory-eastus2-glbl/embeddings"
        "?api-version=2024-10-21"
    )
    assert req.json["input"] == ["a", "b"]
    assert "model" not in req.json


def test_embed_request_openai_compatible_sets_model():
    client = _client(gateway_provider_style="openai_compatible")
    req = client.build_embed_request(deployment="dep-1", inputs=["x"])
    assert req.url == "http://gw.test/openai/embeddings"
    assert req.json["model"] == "dep-1"


async def test_embed_parses_and_orders_vectors():
    def handler(request: httpx.Request) -> httpx.Response:
        # Return out of order to prove the client sorts by index.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            },
        )

    client = _client(transport=httpx.MockTransport(handler))
    vectors = await client.embed(deployment="dep-1", inputs=["first", "second"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


async def test_embed_empty_inputs_short_circuits():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("embed must not call the gateway for empty inputs")

    client = _client(transport=httpx.MockTransport(handler))
    assert await client.embed(deployment="dep-1", inputs=[]) == []


# --- Reasoning-model parameter normalization -------------------------------
# GPT-5 family + o-series reject `max_tokens` (require `max_completion_tokens`)
# and the standard sampling params on the Chat Completions API.

_GPT5 = "gpt-5.2-slurmfactory-eastus2-glbl"
_O4 = "o4-mini-slurmfactory-eastus2-glbl"
_GPT41 = "gpt-4.1-mini-slurmfactory-eastus2-glbl"
_ROUTER = "model-router-slurmfactory-eastus2-glbl"
_DEEPSEEK = "DeepSeek-V3.2-slurmfactory-eastus2-glbl"


@pytest.mark.parametrize("deployment", [_GPT5, _O4])
def test_reasoning_maps_max_tokens_and_strips_sampling(deployment):
    client = _client()
    params = {
        "temperature": 0.7,
        "top_p": 0.9,
        "presence_penalty": 0.5,
        "frequency_penalty": 0.5,
        "logprobs": True,
        "top_logprobs": 3,
        "logit_bias": {"1": 1},
        "max_tokens": 1024,
    }
    body = client.build_request(deployment=deployment, messages=[], params=params).json
    assert body["max_completion_tokens"] == 1024
    assert "max_tokens" not in body
    for key in (
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "logprobs",
        "top_logprobs",
        "logit_bias",
    ):
        assert key not in body


def test_reasoning_keeps_existing_max_completion_tokens():
    client = _client()
    params = {"max_tokens": 1024, "max_completion_tokens": 2048}
    body = client.build_request(deployment=_GPT5, messages=[], params=params).json
    assert body["max_completion_tokens"] == 2048
    assert "max_tokens" not in body


def test_reasoning_drops_none_max_tokens_without_setting_completion():
    client = _client()
    body = client.build_request(
        deployment=_GPT5, messages=[], params={"max_tokens": None}
    ).json
    assert "max_tokens" not in body
    assert "max_completion_tokens" not in body


def test_reasoning_normalization_applies_when_streaming():
    client = _client()
    body = client.build_request(
        deployment=_GPT5,
        messages=[],
        params={"max_tokens": 256, "temperature": 0.2},
        stream=True,
    ).json
    assert body["stream"] is True
    assert body["max_completion_tokens"] == 256
    assert "max_tokens" not in body
    assert "temperature" not in body


@pytest.mark.parametrize("deployment", [_GPT41, _DEEPSEEK])
def test_non_reasoning_params_untouched(deployment):
    client = _client()
    params = {"temperature": 0.7, "top_p": 1, "max_tokens": 1024}
    body = client.build_request(deployment=deployment, messages=[], params=params).json
    assert body["max_tokens"] == 1024
    assert body["temperature"] == 0.7
    assert body["top_p"] == 1
    assert "max_completion_tokens" not in body


def test_model_router_is_not_pre_transformed():
    # model-router accepts the standard param set and drops unsupported params
    # itself when routing to an o-series model, so we must not pre-transform it.
    client = _client()
    params = {"temperature": 0.7, "max_tokens": 1024}
    body = client.build_request(deployment=_ROUTER, messages=[], params=params).json
    assert body["max_tokens"] == 1024
    assert body["temperature"] == 0.7
    assert "max_completion_tokens" not in body


def test_reasoning_prefix_does_not_overmatch():
    # A hypothetical "gpt-50" style id must not be treated as gpt-5.
    client = _client()
    body = client.build_request(
        deployment="gpt-50x-slurmfactory-eastus2-glbl",
        messages=[],
        params={"max_tokens": 10, "temperature": 0.3},
    ).json
    assert body["max_tokens"] == 10
    assert body["temperature"] == 0.3


# --- Responses API path -----------------------------------------------------



def test_messages_to_responses_input_splits_system_and_turns():
    instructions, items = _messages_to_responses_input(
        [
            {"role": "system", "content": "primary"},
            {"role": "system", "content": "memory block"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "again"},
        ]
    )
    # System messages concatenate in order; turns become input items.
    assert instructions == "primary\n\nmemory block"
    assert items == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
    ]


def test_messages_to_responses_input_no_system_is_none():
    instructions, items = _messages_to_responses_input(
        [{"role": "user", "content": "hi"}]
    )
    assert instructions is None
    assert items == [{"role": "user", "content": "hi"}]


def test_normalize_params_for_responses_maps_and_floors():
    # A small max_tokens floors up (reasoning tokens would otherwise truncate to
    # an empty message); reasoning_effort -> reasoning.effort; sampling stripped.
    out = _normalize_params_for_responses(
        {"max_tokens": 100, "reasoning_effort": "high", "temperature": 0.5, "top_p": 1}
    )
    assert out["max_output_tokens"] == 16384
    assert out["reasoning"] == {"effort": "high"}
    assert "temperature" not in out and "top_p" not in out
    assert "max_tokens" not in out and "reasoning_effort" not in out


def test_normalize_params_for_responses_honors_larger_value():
    out = _normalize_params_for_responses({"max_completion_tokens": 50000})
    assert out["max_output_tokens"] == 50000


def test_normalize_params_for_responses_defaults_when_absent():
    out = _normalize_params_for_responses(None)
    assert out["max_output_tokens"] == 16384
    assert "reasoning" not in out


def test_build_responses_request_native_shape():
    client = _client(
        gateway_provider_style="azure_openai_native",
        gateway_api_version="2025-04-01-preview",
    )
    req = client.build_responses_request(
        deployment="gpt-5-pro-slurmfactory-eastus2-glbl",
        messages=[
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
        params={"max_tokens": 200},
        correlation_id="abc",
    )
    # Path is /responses (NOT /deployments/{dep}/...); deployment is model in body.
    assert req.url == "http://gw.test/openai/responses?api-version=2025-04-01-preview"
    assert req.json["model"] == "gpt-5-pro-slurmfactory-eastus2-glbl"
    assert req.json["instructions"] == "be terse"
    assert req.json["input"] == [{"role": "user", "content": "hi"}]
    assert req.json["max_output_tokens"] == 16384
    assert "stream" not in req.json
    assert req.headers["x-correlation-id"] == "abc"


def test_build_responses_request_sets_stream_flag():
    client = _client(gateway_provider_style="azure_openai_native")
    req = client.build_responses_request(
        deployment="dep", messages=[{"role": "user", "content": "x"}], stream=True
    )
    assert req.json["stream"] is True


def test_responses_requests_opt_out_of_provider_side_storage():
    """``store`` defaults to TRUE on the Responses API, unlike chat completions.

    Verified live against Foundry: a request that omits ``store`` comes back with
    ``store: true`` and the whole prompt/output pair stays retrievable from
    ``GET /responses/{id}`` for 30 days. AI4IA keeps conversation state in Cosmos
    scoped per user and re-sends full history each turn instead of chaining with
    ``previous_response_id``, so that retained copy buys nothing and is a second,
    ungoverned store of user content. Asserted for streaming too: the flag has to
    be on the request body, and only one code path builds it.
    """
    client = _client(gateway_provider_style="azure_openai_native")
    for stream in (False, True):
        req = client.build_responses_request(
            deployment="dep",
            messages=[{"role": "user", "content": "x"}],
            stream=stream,
        )
        assert req.json["store"] is False, f"stream={stream} leaks content upstream"


def test_responses_json_to_chat_translation():
    obj = {
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Hello "},
                    {"type": "output_text", "text": "world"},
                ],
            },
        ],
        "usage": {
            "input_tokens": 12,
            "output_tokens": 7,
            "total_tokens": 19,
            "output_tokens_details": {"reasoning_tokens": 4},
        },
    }
    chat = _responses_json_to_chat(obj)
    assert chat["choices"][0]["message"]["content"] == "Hello world"
    assert chat["_responses_status"] == "completed"
    assert chat["usage"]["prompt_tokens"] == 12
    assert chat["usage"]["completion_tokens"] == 7
    assert chat["usage"]["total_tokens"] == 19
    assert chat["usage"]["completion_tokens_details"]["reasoning_tokens"] == 4


def test_responses_json_to_chat_incomplete_keeps_text_and_usage():
    # An incomplete (truncated) turn still spent tokens: text + usage preserved.
    obj = {
        "status": "incomplete",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "partial"}],
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
    }
    chat = _responses_json_to_chat(obj)
    assert chat["choices"][0]["message"]["content"] == "partial"
    assert chat["_responses_status"] == "incomplete"
    assert chat["usage"]["completion_tokens"] == 5


async def test_complete_responses_branch_translates():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            },
        )

    client = _client(
        transport=httpx.MockTransport(handler),
        gateway_provider_style="azure_openai_native",
    )
    result = await client.complete(
        deployment="dep-1",
        messages=[{"role": "user", "content": "x"}],
        api="responses",
    )
    assert result["choices"][0]["message"]["content"] == "ok"
    assert result["usage"]["prompt_tokens"] == 2
    assert "/responses" in captured["url"]
    assert captured["body"]["model"] == "dep-1"


async def test_complete_responses_failed_status_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "failed", "error": {"message": "content blocked"}},
        )

    client = _client(
        transport=httpx.MockTransport(handler),
        gateway_provider_style="azure_openai_native",
    )
    with pytest.raises(ModelGatewayError) as exc:
        await client.complete(deployment="dep-1", messages=[], api="responses")
    assert exc.value.status_code == 502
    assert "content blocked" in exc.value.detail


def test_parse_responses_event_delta_is_chat_shaped():
    chunk = _parse_responses_event(
        json.dumps({"type": "response.output_text.delta", "delta": "hi"})
    )
    assert chunk is not None
    assert chunk.delta == "hi"
    # raw mirrors the chat-completions delta shape the frontend already parses.
    assert json.loads(chunk.raw) == {"choices": [{"delta": {"content": "hi"}}]}
    assert chunk.done is False


def test_parse_responses_event_completed_is_terminal_with_usage():
    chunk = _parse_responses_event(
        json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}
                },
            }
        )
    )
    assert chunk is not None
    assert chunk.done is True
    assert chunk.usage == {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "total_tokens": 7,
    }


def test_parse_responses_event_incomplete_is_terminal_not_error():
    chunk = _parse_responses_event(
        json.dumps(
            {
                "type": "response.incomplete",
                "response": {
                    "usage": {"input_tokens": 5, "output_tokens": 9, "total_tokens": 14}
                },
            }
        )
    )
    assert chunk is not None
    assert chunk.done is True
    assert chunk.usage["completion_tokens"] == 9


def test_parse_responses_event_failed_raises():
    with pytest.raises(ModelGatewayError) as exc:
        _parse_responses_event(
            json.dumps(
                {
                    "type": "response.failed",
                    "response": {"error": {"message": "boom"}},
                }
            )
        )
    assert exc.value.status_code == 502
    assert "boom" in exc.value.detail


def test_parse_responses_event_ignores_noise_events():
    assert _parse_responses_event(json.dumps({"type": "response.created"})) is None
    assert _parse_responses_event("") is None
    assert _parse_responses_event("not json") is None


async def test_stream_responses_yields_deltas_then_terminal_usage():
    sse = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Hel"}\n'
        "\n"
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"lo"}\n'
        "\n"
        "event: response.completed\n"
        'data: {"type":"response.completed","response":'
        '{"usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}\n'
        "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=sse)

    client = _client(
        transport=httpx.MockTransport(handler),
        gateway_provider_style="azure_openai_native",
    )
    chunks = [
        c
        async for c in client.stream(
            deployment="dep-1",
            messages=[{"role": "user", "content": "x"}],
            api="responses",
        )
    ]
    text = "".join(c.delta for c in chunks)
    assert text == "Hello"
    assert chunks[-1].done is True
    assert chunks[-1].usage == {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "total_tokens": 7,
    }


async def test_stream_responses_incomplete_terminates_cleanly():
    sse = (
        'data: {"type":"response.output_text.delta","delta":"part"}\n'
        "\n"
        'data: {"type":"response.incomplete","response":'
        '{"usage":{"input_tokens":3,"output_tokens":9,"total_tokens":12}}}\n'
        "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    client = _client(
        transport=httpx.MockTransport(handler),
        gateway_provider_style="azure_openai_native",
    )
    chunks = [
        c async for c in client.stream(deployment="d", messages=[], api="responses")
    ]
    assert "".join(c.delta for c in chunks) == "part"
    assert chunks[-1].done is True
    assert chunks[-1].usage["completion_tokens"] == 9


async def test_stream_responses_failed_raises():
    sse = (
        'data: {"type":"response.failed","response":'
        '{"error":{"message":"stream boom"}}}\n'
        "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    client = _client(
        transport=httpx.MockTransport(handler),
        gateway_provider_style="azure_openai_native",
    )
    with pytest.raises(ModelGatewayError) as exc:
        [c async for c in client.stream(deployment="d", messages=[], api="responses")]
    assert exc.value.status_code == 502
    assert "stream boom" in exc.value.detail


async def test_stream_responses_accumulates_multiline_data():
    # A single SSE frame whose JSON payload spans multiple data: lines must be
    # joined before parsing.
    sse = (
        'data: {"type":"response.output_text.delta",\n'
        'data: "delta":"hi"}\n'
        "\n"
        'data: {"type":"response.completed","response":{"usage":'
        '{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n'
        "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    client = _client(
        transport=httpx.MockTransport(handler),
        gateway_provider_style="azure_openai_native",
    )
    chunks = [
        c async for c in client.stream(deployment="d", messages=[], api="responses")
    ]
    assert "".join(c.delta for c in chunks) == "hi"
    assert chunks[-1].done is True


async def test_stream_responses_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    client = _client(
        transport=httpx.MockTransport(handler),
        gateway_provider_style="azure_openai_native",
    )
    with pytest.raises(ModelGatewayError) as exc:
        [c async for c in client.stream(deployment="d", messages=[], api="responses")]
    assert exc.value.status_code == 429
