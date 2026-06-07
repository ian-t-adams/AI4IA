import httpx
import pytest

from ai4ia_api.gateway.client import ModelGatewayClient, ModelGatewayError, parse_sse_line
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


def test_stream_flag_added_only_when_streaming():
    client = _client()
    assert "stream" not in client.build_request(deployment="d", messages=[]).json
    assert client.build_request(deployment="d", messages=[], stream=True).json["stream"] is True


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
