from __future__ import annotations

import base64
import json

import httpx

from ai4ia_api.gateway.client import ModelGatewayClient
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
