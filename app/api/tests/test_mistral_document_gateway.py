from __future__ import annotations

import base64
import json

import httpx
import pytest

from ai4ia_api.catalog import DeploymentOption
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.library.mistral_document import (
    MistralDocumentClient,
    MistralDocumentError,
)
from tests.conftest import make_settings


def test_mistral_document_request_keeps_proxy_routing_and_inlines_bytes():
    client = ModelGatewayClient(make_settings())
    request = client.build_document_ocr_request(
        deployment="mistral-document-ai-2512-slurmfactory-eastus2-glbl",
        data=b"image-bytes",
        content_type="image/png",
        correlation_id="corr",
    )

    assert request.url.endswith(
        "/deployments/mistral-document-ai-2512-slurmfactory-eastus2-glbl"
        "/document/ocr?api-version=2025-04-01-preview"
    )
    assert request.headers["x-correlation-id"] == "corr"
    assert request.json["model"] == (
        "mistral-document-ai-2512-slurmfactory-eastus2-glbl"
    )
    assert request.json["document"] == {
        "type": "image_url",
        "image_url": (
            "data:image/png;base64,"
            + base64.b64encode(b"image-bytes").decode("ascii")
        ),
    }


async def test_mistral_document_response_is_returned():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["document"]["type"] == "document_url"
        return httpx.Response(
            200,
            json={"pages": [{"markdown": "# Parsed"}]},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ModelGatewayClient(make_settings(), http_client=http)
    try:
        result = await client.analyze_document(
            deployment="mistral-ocr-4-0-slurmfactory-eastus2-glbl",
            data=b"%PDF",
            content_type="application/pdf",
        )
    finally:
        await http.aclose()

    assert result["pages"][0]["markdown"] == "# Parsed"


class _Catalog:
    def resolve_deployment(self, _model_id: str):
        return DeploymentOption(
            region="eastus2",
            dataZone="us",
            sku="GlobalStandard",
            deploymentName="mistral-deployment",
        )


class _RejectingGateway:
    async def analyze_document(self, **_kwargs):
        raise ModelGatewayError(
            400,
            '{"message":"Received data:image/png;base64,PRIVATE_DOCUMENT_BYTES"}',
        )


async def test_provider_error_cannot_persist_or_log_echoed_document_bytes():
    client = MistralDocumentClient(_RejectingGateway(), _Catalog())

    with pytest.raises(MistralDocumentError) as captured:
        await client.analyze(
            "mistral-document-ai-2512",
            b"PRIVATE_DOCUMENT_BYTES",
            "image/png",
        )

    assert str(captured.value) == "Mistral document request failed (status=400)."
    assert "PRIVATE_DOCUMENT_BYTES" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.provider_completed is True


async def test_malformed_success_body_becomes_a_provider_error_not_a_crash():
    """A 2xx with a non-JSON body must not escape as a raw JSONDecodeError.

    The provider ran (and will bill) but the result is unreadable. Surfacing it as
    a gateway error keeps ingest on its normal terminal-failure path and records a
    provider-completed attempt, instead of an opaque decode crash.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>gateway timeout page</html>")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ModelGatewayClient(make_settings(), http_client=http)
    try:
        with pytest.raises(ModelGatewayError) as captured:
            await client.analyze_document(
                deployment="mistral-ocr-4-0-slurmfactory-eastus2-glbl",
                data=b"%PDF",
                content_type="application/pdf",
            )
    finally:
        await http.aclose()

    assert captured.value.status_code == 502
    assert captured.value.detail == "malformed provider document response"


async def test_provider_error_body_is_bounded_at_the_gateway():
    """Defense in depth: the raw provider body never travels whole.

    The Mistral client already reduces this to a status-only message, but the
    gateway is the boundary where an arbitrarily long base64 echo would otherwise
    be copied into an exception that other call sites might log.
    """
    echoed = "data:image/png;base64," + ("A" * 5000)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=echoed)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ModelGatewayClient(make_settings(), http_client=http)
    try:
        with pytest.raises(ModelGatewayError) as captured:
            await client.analyze_document(
                deployment="mistral-ocr-4-0-slurmfactory-eastus2-glbl",
                data=b"%PDF",
                content_type="application/pdf",
            )
    finally:
        await http.aclose()

    assert len(captured.value.detail) <= 200
