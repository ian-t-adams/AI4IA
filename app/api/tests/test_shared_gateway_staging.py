"""Shared gateway preparation never selects an unverified transport."""
from __future__ import annotations

import httpx
import pytest

from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app
from tests.conftest import make_settings


@pytest.mark.parametrize("configured", [False, True])
def test_shared_route_off_preserves_existing_requests(configured: bool):
    baseline = make_settings()
    staged = make_settings(
        shared_gateway_contract_version="1" if configured else None,
        shared_gateway_url="https://unverified.example/unused" if configured else None,
        shared_gateway_issuer="https://unverified-issuer.example" if configured else None,
        shared_gateway_audience="unverified-audience" if configured else None,
    )
    assert staged.shared_gateway_enabled is False
    staged.validate_runtime()
    original = ModelGatewayClient(baseline)
    prepared = ModelGatewayClient(staged)
    for stream in (False, True):
        assert prepared.build_request(
            deployment="synthetic-deployment",
            messages=[{"role": "user", "content": "synthetic prompt"}],
            stream=stream,
            correlation_id="synthetic-correlation",
        ) == original.build_request(
            deployment="synthetic-deployment",
            messages=[{"role": "user", "content": "synthetic prompt"}],
            stream=stream,
            correlation_id="synthetic-correlation",
        )
    assert prepared.image_edit_url("synthetic-deployment") == original.image_edit_url(
        "synthetic-deployment"
    )
    assert prepared.transcription_url("synthetic-deployment") == original.transcription_url(
        "synthetic-deployment"
    )


@pytest.mark.parametrize(
    ("url", "audience", "message"),
    [
        (None, None, "explicitly configured"),
        ("https://unverified.example", None, "explicitly configured"),
        (None, "unverified-audience", "explicitly configured"),
        ("https://unverified.example", "unverified-audience", "not been implemented"),
    ],
)
def test_shared_route_on_refuses_startup_and_client_construction(url, audience, message):
    settings = make_settings(
        shared_gateway_enabled=True,
        shared_gateway_contract_version="1",
        shared_gateway_url=url,
        shared_gateway_issuer="https://unverified-issuer.example",
        shared_gateway_audience=audience,
    )
    with pytest.raises(RuntimeError, match=message):
        settings.validate_runtime()
    with pytest.raises(RuntimeError, match=message):
        create_app(settings)
    with pytest.raises(RuntimeError, match=message):
        ModelGatewayClient(settings)


async def test_unverified_shared_route_refuses_before_any_http_or_fallback():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"choices": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        enabled = make_settings(
            shared_gateway_enabled=True,
            shared_gateway_contract_version="1",
            shared_gateway_url="https://unverified.example",
            shared_gateway_issuer="https://unverified-issuer.example",
            shared_gateway_audience="unverified-audience",
        )
        with pytest.raises(RuntimeError, match="not been implemented"):
            ModelGatewayClient(enabled, http_client=http)
        assert calls == []

        disabled = make_settings(
            shared_gateway_enabled=False,
            shared_gateway_url=enabled.shared_gateway_url,
            shared_gateway_audience=enabled.shared_gateway_audience,
        )
        client = ModelGatewayClient(disabled, http_client=http)
        await client.complete(deployment="synthetic-deployment", messages=[])
        assert len(calls) == 1
        assert str(calls[0].url).startswith(disabled.model_gateway_url)
        assert calls[0].url.host != "unverified.example"


def test_shared_route_errors_do_not_echo_configuration_or_credentials():
    settings = make_settings(
        shared_gateway_enabled=True,
        shared_gateway_contract_version="1",
        shared_gateway_url="https://user:synthetic-password@unverified.example/secret-path",
        shared_gateway_issuer="https://synthetic-private-issuer.example",
        shared_gateway_audience="synthetic-private-audience",
        model_gateway_api_key="synthetic-proxy-secret",
    )
    with pytest.raises(RuntimeError) as error:
        ModelGatewayClient(settings)
    message = str(error.value)
    for value in (
        settings.shared_gateway_url,
        settings.shared_gateway_issuer,
        settings.shared_gateway_audience,
        settings.model_gateway_api_key,
    ):
        assert value not in message


@pytest.mark.parametrize("version", [None, "", "0", "2", "unknown"])
def test_missing_or_unknown_contract_version_cannot_select_shared_route(version):
    settings = make_settings(
        shared_gateway_enabled=True,
        shared_gateway_contract_version=version,
        shared_gateway_url="https://unverified.example",
        shared_gateway_issuer="https://unverified-issuer.example",
        shared_gateway_audience="unverified-audience",
    )
    with pytest.raises(RuntimeError, match="must explicitly select 1"):
        settings.validate_runtime()
    with pytest.raises(RuntimeError, match="must explicitly select 1"):
        ModelGatewayClient(settings)


def test_missing_issuer_cannot_select_shared_route():
    settings = make_settings(
        shared_gateway_enabled=True,
        shared_gateway_contract_version="1",
        shared_gateway_url="https://unverified.example",
        shared_gateway_audience="unverified-audience",
    )
    with pytest.raises(RuntimeError, match="explicitly configured"):
        settings.validate_runtime()
