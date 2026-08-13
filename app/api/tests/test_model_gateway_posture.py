"""Production model traffic must enter through server-owned SimpleL7Proxy."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import make_settings


def _production_settings(*, gateway_url: str, **overrides):
    values = {
        "env": "prod",
        "auth_provider": "entra",
        "entra_tenant_id": "tenant-1",
        "entra_audience": "api-client",
        "model_gateway_url": gateway_url,
        "model_gateway_auth_mode": "api_key",
        "model_gateway_api_key": "proxy-ingress-secret",
        "model_gateway_api_key_header": "S7P-KEY",
        "model_gateway_allowed_hosts": "proxy.internal.example",
    }
    values.update(overrides)
    return make_settings(**values)


def test_prod_allows_server_owned_simplel7proxy_ingress():
    settings = _production_settings(
        gateway_url="https://proxy.internal.example/openai"
    )

    settings.validate_runtime()


@pytest.mark.parametrize(
    "gateway_url",
    [
        "https://ca-proxy-env.azurecontainerapps.io/openai",
        "https://models.example.com/openai",
    ],
)
def test_prod_allows_each_server_configured_proxy_host(gateway_url: str):
    settings = _production_settings(
        gateway_url=gateway_url,
        model_gateway_allowed_hosts=(
            "ca-proxy-env.azurecontainerapps.io,models.example.com"
        ),
    )

    settings.validate_runtime()


def test_prod_rejects_random_public_https_openai_host():
    settings = _production_settings(
        gateway_url="https://evil.example/openai",
        model_gateway_allowed_hosts="proxy.internal.example",
    )

    with pytest.raises(RuntimeError, match="ALLOWED_HOSTS"):
        settings.validate_runtime()


@pytest.mark.parametrize(
    "gateway_url",
    [
        "https://models.openai.azure.com/openai",
        "https://project.services.ai.azure.com/openai",
        "https://resource.cognitiveservices.azure.com/openai",
        "https://shared-apim.azure-api.net/openai",
    ],
)
def test_prod_rejects_known_direct_model_and_apim_hosts(gateway_url: str):
    host = gateway_url.split("/", 3)[2]
    settings = _production_settings(
        gateway_url=gateway_url,
        model_gateway_allowed_hosts=host,
    )

    with pytest.raises(RuntimeError, match="SimpleL7Proxy"):
        settings.validate_runtime()


@pytest.mark.parametrize(
    ("gateway_url", "overrides"),
    [
        ("http://proxy.internal.example/openai", {}),
        ("https://proxy.internal.example", {}),
        ("https://proxy.internal.example/openai?api-version=preview", {}),
        (
            "https://proxy.internal.example/openai",
            {"model_gateway_api_key_header": "Ocp-Apim-Subscription-Key"},
        ),
        (
            "https://proxy.internal.example/openai",
            {"model_gateway_auth_mode": "bearer"},
        ),
    ],
)
def test_prod_rejects_non_proxy_bypass_shapes(
    gateway_url: str, overrides: dict[str, str]
):
    settings = _production_settings(gateway_url=gateway_url, **overrides)

    with pytest.raises(RuntimeError, match="SimpleL7Proxy"):
        settings.validate_runtime()


def test_non_prod_preserves_local_test_gateway_flexibility():
    settings = make_settings(
        env="local",
        model_gateway_url="https://models.openai.azure.com/openai",
        model_gateway_auth_mode="none",
        model_gateway_api_key_header="Ocp-Apim-Subscription-Key",
    )

    settings.validate_runtime()


def test_deployed_dev_allows_only_the_server_owned_proxy():
    settings = _production_settings(
        gateway_url="https://proxy.internal.example/openai",
        env="dev",
        auth_provider="dev",
        allow_dev_auth=True,
    )

    settings.validate_runtime()


def test_deployed_dev_rejects_direct_foundry_even_with_dev_auth_enabled():
    settings = _production_settings(
        gateway_url="https://project.services.ai.azure.com/openai",
        model_gateway_allowed_hosts="project.services.ai.azure.com",
        env="dev",
        auth_provider="dev",
        allow_dev_auth=True,
    )

    with pytest.raises(RuntimeError, match="SimpleL7Proxy"):
        settings.validate_runtime()


def test_iac_derives_and_injects_default_and_custom_proxy_hosts():
    repo = Path(__file__).resolve().parents[3]
    gateway = (repo / "infra" / "modules" / "gateway.bicep").read_text(
        encoding="utf-8"
    )
    main = (repo / "infra" / "main.bicep").read_text(encoding="utf-8")
    api = (repo / "infra" / "modules" / "api.bicep").read_text(
        encoding="utf-8"
    )

    assert "[ proxyFqdn ]" in gateway
    assert "[ toLower(trim(customDomain)) ]" in gateway
    assert (
        "output proxyIngressHosts string = join(effectiveProxyIngressHosts, ',')"
        in gateway
    )
    assert (
        "modelGatewayAllowedHosts: gateway.outputs.proxyIngressHosts"
        in main
    )
    assert "name: 'AI4IA_MODEL_GATEWAY_ALLOWED_HOSTS'" in api
    assert "value: modelGatewayAllowedHosts" in api
