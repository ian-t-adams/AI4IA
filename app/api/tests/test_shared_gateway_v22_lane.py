"""Synthetic 2.2.0 isolated-lane contract: /<lane>/v1 prefix, non-pilot only."""
from __future__ import annotations

import json

import httpx
import pytest

from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.shared_gateway_contract import (
    SharedGatewayManifestError,
    load_shared_gateway_manifest,
)
from tests.test_shared_gateway_staging import DEPLOYMENT, _settings, _token
from tests.test_shared_gateway_v21 import resolved

LANE = "/ai4ia-prod/v1"


def lane(prefix: str = LANE, base_prefix: str | None = None) -> dict:
    value = resolved()
    value["contractVersion"] = "2.2.0"
    value["gateway"] = {
        "baseUrl": f"https://gateway.test{prefix if base_prefix is None else base_prefix}",
        "allowedHosts": ["gateway.test"],
        "pathPrefix": prefix,
    }
    for capability in value["capabilities"].values():
        capability["enabled"] = False
        capability["models"] = {}
    value["models"] = {
        DEPLOYMENT: {
            "kind": "deployment-name",
            "operations": ["/chat/completions", "/responses"],
            "targets": [{"deployment": DEPLOYMENT, "sku": "GlobalStandard"}],
        },
    }
    return value


def load(value: dict, *, pilot_mode: bool = False):
    return load_shared_gateway_manifest(
        json.dumps(value),
        api_identity_client_id=value["auth"]["callerClientId"],
        pilot_mode=pilot_mode,
    )


def test_lane_prefix_loads_and_resolves_opted_in_deployment():
    manifest = load(lane())
    assert manifest.contract_version == "2.2.0"
    assert manifest.base_url == "https://gateway.test/ai4ia-prod/v1"
    assert manifest.resolve_shared_model_key(
        DEPLOYMENT, "GlobalStandard", "/responses",
    ) == DEPLOYMENT


async def test_lane_request_uses_single_v1_under_the_lane_prefix():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "lane"}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await ModelGatewayClient(
            _settings(manifest=lane()).model_copy(
                update={"model_gateway_allowed_hosts": "existing-gateway.example.invalid"},
            ),
            http_client=http,
            shared_gateway_token_provider=_token,
        ).complete(
            deployment=DEPLOYMENT,
            messages=[{"role": "user", "content": "synthetic"}],
            shared_gateway_eligible=True,
            shared_gateway_sku="GlobalStandard",
        )

    assert [str(r.url) for r in requests] == [
        "https://gateway.test/ai4ia-prod/v1/chat/completions",
    ]


@pytest.mark.parametrize(
    ("prefix", "base_prefix"),
    [
        ("/ai4ia-prod/v1/v1", None),
        ("/ai4ia-prod", None),
        ("/ai4ia-prod/../v1", None),
        ("/ai4ia.prod/v1", None),
        ("/AI4IA-prod/v1", None),
        ("/ai4ia-prod/v1/", None),
        ("/-ai4ia/v1", None),
        ("/ai4ia-prod/v1", "/other-lane/v1"),
        ("/ai4ia-prod/v1", "/ai4ia-prod/v1/"),
    ],
)
def test_lane_prefix_rejects_malformed_or_mismatched_paths(prefix, base_prefix):
    with pytest.raises(SharedGatewayManifestError):
        load(lane(prefix, base_prefix))


def test_lane_contract_is_rejected_for_the_isolated_pilot():
    with pytest.raises(SharedGatewayManifestError):
        load(lane(), pilot_mode=True)


@pytest.mark.parametrize(
    "mutation",
    ["family-url", "enabled", "models"],
)
def test_lane_forbids_family_capabilities(mutation):
    value = lane()
    family = value["capabilities"]["claudeMessages"]
    if mutation == "family-url":
        value["gateway"]["anthropicBaseUrl"] = "https://gateway.test/anthropic/v1"
    elif mutation == "enabled":
        family["enabled"] = True
    else:
        family["models"] = resolved()["capabilities"]["claudeMessages"]["models"]
    with pytest.raises(SharedGatewayManifestError):
        load(value)


@pytest.mark.parametrize("version", ["2.0.0", "2.1.0"])
def test_earlier_contracts_keep_rejecting_lane_prefix(version):
    value = lane()
    value["contractVersion"] = version
    with pytest.raises(SharedGatewayManifestError):
        load(value)
