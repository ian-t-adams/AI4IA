"""Wire shape of the governed ``images/edits`` gateway call.

A MockTransport captures the exact outbound request, so these tests prove the
multipart body carries the source bytes unchanged, that the dedicated edit
api-version is used, and that refusals happen before anything is sent.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from ai4ia_api.config import GatewayAuthMode, GatewayProviderStyle
from ai4ia_api.gateway import client as client_module
from ai4ia_api.gateway.client import ModelGatewayClient, ModelGatewayError
from tests.conftest import make_settings

SOURCE = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4 + b"\r\n--not-a-boundary\r\n"
MASK = b"\x89PNG\r\n\x1a\nmask" + b"\x00\xff" * 64


def _parts(request: httpx.Request) -> dict[str, tuple[dict[str, str], bytes]]:
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1].encode()
    body = request.content
    assert body.endswith(b"--" + boundary + b"--\r\n")
    out: dict[str, tuple[dict[str, str], bytes]] = {}
    for chunk in body.split(b"--" + boundary)[1:-1]:
        head, _, data = chunk[2:].partition(b"\r\n\r\n")
        assert data.endswith(b"\r\n")
        headers = dict(
            line.split(": ", 1) for line in head.decode().split("\r\n") if line
        )
        name = headers["Content-Disposition"].split('name="', 1)[1].split('"', 1)[0]
        out[name] = (headers, data[:-2])
    return out


async def _edit(settings_overrides=None, reply=None, **kwargs):
    sent: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return reply or httpx.Response(200, json={"data": [{"b64_json": "aGk="}], "usage": None})

    settings = make_settings(**(settings_overrides or {}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        gateway = ModelGatewayClient(settings, http)
        result = await gateway.edit_image(**{
            "deployment": "gpt-image-2.5-sunburst-dep", "prompt": "make it blue",
            "image": SOURCE, "image_content_type": "image/png", **kwargs,
        })
    return sent, result


async def test_native_edit_is_multipart_on_the_deployment_path_with_edit_api_version():
    sent, result = await _edit(
        {"gateway_api_version": "2099-chat", "gateway_image_api_version": "2024-10-21"},
        mask=MASK, size="1536x1024", quality="high",
    )
    assert result["data"][0]["b64_json"] == "aGk="
    (request,) = sent
    assert str(request.url) == (
        "http://gateway.test/deployments/gpt-image-2.5-sunburst-dep/images/edits"
        "?api-version=2025-04-01-preview"
    )
    parts = _parts(request)
    assert set(parts) == {"prompt", "n", "size", "quality", "image", "mask"}
    headers, data = parts["image"]
    # Byte-exact: the multipart boundary-like and CRLF bytes in the source survive.
    assert data == SOURCE
    assert headers["Content-Type"] == "image/png"
    assert parts["mask"][1] == MASK
    assert parts["mask"][0]["Content-Type"] == "image/png"
    assert parts["prompt"][1] == b"make it blue"
    assert (parts["n"][1], parts["size"][1], parts["quality"][1]) == (b"1", b"1536x1024", b"high")
    # Azure-native carries the deployment in the path, never a model field.
    assert "model" not in parts


async def test_whole_image_edit_omits_mask_and_auto_controls():
    sent, _ = await _edit(image_content_type="image/jpeg")
    parts = _parts(sent[0])
    assert set(parts) == {"prompt", "n", "image"}
    assert parts["image"][0]["Content-Type"] == "image/jpeg"
    assert 'filename="image.jpg"' in parts["image"][0]["Content-Disposition"]


async def test_openai_compatible_edit_names_the_deployment_as_model():
    sent, _ = await _edit({"gateway_provider_style": GatewayProviderStyle.openai_compatible})
    assert str(sent[0].url) == "http://gateway.test/images/edits"
    assert _parts(sent[0])["model"][1] == b"gpt-image-2.5-sunburst-dep"


async def test_multipart_edit_keeps_gateway_auth_without_a_json_content_type():
    sent, _ = await _edit({
        "model_gateway_auth_mode": GatewayAuthMode.api_key, "model_gateway_api_key": "k-9",
    })
    assert sent[0].headers["Ocp-Apim-Subscription-Key"] == "k-9"
    assert "application/json" not in sent[0].headers["content-type"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"api": "mai"}, "Azure OpenAI image surface"),
        ({"api": "bfl"}, "Azure OpenAI image surface"),
        ({"image_content_type": "image/webp"}, "PNG or JPEG"),
        ({"image_content_type": "image/gif"}, "PNG or JPEG"),
    ],
)
async def test_unsupported_edit_is_refused_before_any_send(kwargs, match):
    with pytest.raises(ValueError, match=match):
        await _edit(**kwargs)
    # Paired control: the same call with the governed values is sent.
    sent, _ = await _edit()
    assert len(sent) == 1


async def test_provider_error_raises_the_gateway_error():
    with pytest.raises(ModelGatewayError) as caught:
        await _edit(reply=httpx.Response(400, json={"error": {"code": "contentFilter"}}))
    assert caught.value.status_code == 400


async def test_admission_sees_a_digest_descriptor_never_the_file_bytes(monkeypatch):
    seen: dict = {}
    real = client_module.admitted_dispatch

    @asynccontextmanager
    async def spy(surface, payload, **kwargs):
        seen.update(surface=surface, payload=payload)
        async with real(surface, payload, **kwargs) as lease:
            yield lease

    monkeypatch.setattr(client_module, "admitted_dispatch", spy)
    await _edit(mask=MASK, size="1024x1024")
    assert seen["surface"] == "image"
    payload = seen["payload"]
    assert payload["operation"] == "images/edits"
    assert payload["image"] == {
        "sha256": hashlib.sha256(SOURCE).hexdigest(), "bytes": len(SOURCE),
        "contentType": "image/png",
    }
    assert payload["mask"] == {"sha256": hashlib.sha256(MASK).hexdigest(), "bytes": len(MASK)}
    serialized = json.dumps(payload)
    assert "PNG" not in serialized and "\\u0000" not in serialized
