"""Pure URL/body shaping tests for image-generation gateway requests.

No network: only the request builder is exercised, mirroring the chat/embeddings
builder tests. Guards the two provider styles, the dedicated image api-version,
and the param/auth shaping.
"""
from __future__ import annotations

from ai4ia_api.config import GatewayAuthMode, GatewayProviderStyle
from ai4ia_api.gateway.client import ModelGatewayClient
from tests.conftest import make_settings


def _client(**overrides) -> ModelGatewayClient:
    return ModelGatewayClient(make_settings(**overrides))


def test_azure_native_image_url_and_body():
    c = _client(gateway_image_api_version="2024-10-21")
    req = c.build_image_request(deployment="gpt-image-2-dep", prompt="a cat", size="1024x1024")
    assert req.url == (
        "http://gateway.test/deployments/gpt-image-2-dep/images/generations"
        "?api-version=2024-10-21"
    )
    assert req.json["prompt"] == "a cat"
    assert req.json["n"] == 1
    assert req.json["size"] == "1024x1024"
    # Azure-native carries the deployment in the path, never the body.
    assert "model" not in req.json


def test_image_uses_image_api_version_not_chat():
    c = _client(
        gateway_api_version="2099-chat-only",
        gateway_image_api_version="2024-10-21",
    )
    req = c.build_image_request(deployment="dep", prompt="x")
    assert "api-version=2024-10-21" in req.url
    assert "2099-chat-only" not in req.url


def test_openai_compatible_image_puts_model_in_body():
    c = _client(gateway_provider_style=GatewayProviderStyle.openai_compatible)
    req = c.build_image_request(deployment="gpt-image-2-dep", prompt="hi", n=1)
    assert req.url == "http://gateway.test/images/generations"
    assert req.json["model"] == "gpt-image-2-dep"
    assert "api-version" not in req.url


def test_size_omitted_when_none():
    c = _client()
    req = c.build_image_request(deployment="dep", prompt="x", size=None)
    assert "size" not in req.json


def test_extra_params_are_merged():
    c = _client()
    req = c.build_image_request(
        deployment="dep", prompt="x", extra={"quality": "high", "output_format": "png"}
    )
    assert req.json["quality"] == "high"
    assert req.json["output_format"] == "png"


def test_bfl_image_request_uses_server_owned_native_shape():
    c = _client(gateway_image_api_version="2026-image")
    req = c.build_image_request(
        deployment="FLUX.2-pro-slurmfactory-eastus2-glbl",
        prompt="a red fox",
        size="1536x1024",
        n=1,
        extra={"quality": "high", "model": "attacker"},
        api="bfl",
    )
    assert req.url.endswith(
        "/deployments/FLUX.2-pro-slurmfactory-eastus2-glbl/images/generations"
        "?api-version=2026-image"
    )
    assert req.json == {
        "model": "flux.2-pro-slurmfactory-eastus2-glbl",
        "prompt": "a red fox",
        "num_images": 1,
        "output_format": "png",
        "safety_tolerance": 2,
        "width": 1536,
        "height": 1024,
    }


def test_api_key_auth_header_present():
    c = _client(
        model_gateway_auth_mode=GatewayAuthMode.api_key,
        model_gateway_api_key="k-123",
    )
    req = c.build_image_request(deployment="dep", prompt="x")
    assert req.headers["Ocp-Apim-Subscription-Key"] == "k-123"
