"""The agent-callable ``generate_image`` capability + artifact serve endpoint.

Covers the synthetic tool's core contract (generate → persist → media-ref sink →
meter), the per-user ownership boundary of the authenticated serve endpoint, and
that a generated-image reference round-trips through ``Message`` serialization
(how attachments survive the Cosmos store + the messages-list response model).
"""
from __future__ import annotations

import asyncio
import base64
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.tool_exec import ToolContext
from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.images.capability import (
    GENERATE_IMAGE_TOOL_NAME,
    MAX_IMAGES_PER_TURN,
    build_image_capability,
)
from ai4ia_api.images.service import ImageGenerationService
from ai4ia_api.main import create_app
from ai4ia_api.sessions.models import Message, MessageAttachment, MessageRole
from tests.conftest import make_settings
from tests.test_image_api import TINY_PNG_B64, FakeImageGateway


def _client() -> TestClient:
    app = create_app(make_settings(admin_subjects="alice"))
    c = TestClient(app)
    c.__enter__()
    c.app.state.gateway = FakeImageGateway()
    return c


@pytest.fixture
def client():
    c = _client()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _internal_id(client, headers) -> str:
    return client.get("/api/entitlement", headers=headers).json()["userId"]


def _build_capability(client, user_id: str, sink: list[MessageAttachment]):
    service = ImageGenerationService(
        catalog=client.app.state.catalog, gateway=client.app.state.gateway
    )
    tools, handlers = build_image_capability(
        image_service=service,
        artifact_store=client.app.state.image_artifacts,
        entitlements=client.app.state.entitlements,
        metering=client.app.state.usage,
        catalog=client.app.state.catalog,
        user_id=user_id,
        session_id="s-img",
        sink=sink,
    )
    return tools, handlers


# ---- capability handler ----


def test_handler_generates_persists_sinks_and_meters(client):
    headers = {"X-Dev-User": "ian"}
    uid = _internal_id(client, headers)
    sink: list[MessageAttachment] = []
    tools, handlers = _build_capability(client, uid, sink)

    # The tool advertises exactly the generate_image function schema.
    assert tools[0]["function"]["name"] == GENERATE_IMAGE_TOOL_NAME
    handler = handlers[GENERATE_IMAGE_TOOL_NAME]

    out = asyncio.run(
        handler({"prompt": "a red bird", "model": "gpt-image-2"}, ToolContext())
    )
    assert out["status"] == "generated"
    artifact_id = out["artifact_id"]
    assert out["model"] == "gpt-image-2"
    # The base64 pixels never come back through the tool result (8 KB cap): no
    # known image-payload key carries the encoded pixels, and the actual
    # encoded payload never appears in any value. (A prior
    # `"b64" not in str(out).lower()` substring check over the whole dict was
    # flaky: artifact_id is a random `uuid4().hex` string, and "b64" is
    # itself a valid 3-hex-digit substring - e.g. "...7db646..." - that turns
    # up by chance in roughly 1 of every ~140 random ids.)
    forbidden_payload_keys = {"b64_json", "b64", "images_b64", "image_b64", "data"}
    assert not forbidden_payload_keys & out.keys()
    assert all(TINY_PNG_B64 not in str(value) for value in out.values())

    # A media reference was appended for the chat router to attach.
    assert len(sink) == 1
    att = sink[0]
    assert att.id == artifact_id
    assert att.kind == "image"
    assert att.prompt == "a red bird"

    # The bytes are durably stored, owner-scoped, and decode to the canned PNG.
    stored = asyncio.run(client.app.state.image_artifacts.get(uid, artifact_id))
    assert stored == base64.b64decode(TINY_PNG_B64)

    # The call was metered into the usage ledger (rate/budget windows see it).
    summary = client.get("/api/usage", headers=headers).json()
    assert summary["totalRequests"] >= 1


def test_successful_provider_attempt_is_metered_once_as_complete(client):
    uid = _internal_id(client, {"X-Dev-User": "ian"})
    sink: list[MessageAttachment] = []
    with patch.object(
        client.app.state.usage,
        "record_completion",
        new_callable=AsyncMock,
    ) as meter:
        _, handlers = _build_capability(client, uid, sink)
        out = asyncio.run(
            handlers[GENERATE_IMAGE_TOOL_NAME](
                {"prompt": "a red bird", "model": "gpt-image-2"},
                ToolContext(),
            )
        )

    assert out["status"] == "generated"
    meter.assert_awaited_once()
    assert meter.await_args.kwargs["status"] == "complete"
    assert meter.await_args.kwargs["provider_completed"] is True


def test_decode_failure_is_metered_once_as_error(client):
    class InvalidBase64Gateway(FakeImageGateway):
        async def generate_image(self, **kwargs):
            result = await super().generate_image(**kwargs)
            result["data"][0]["b64_json"] = "a"
            return result

    client.app.state.gateway = InvalidBase64Gateway()
    uid = _internal_id(client, {"X-Dev-User": "ian"})
    sink: list[MessageAttachment] = []
    with patch.object(
        client.app.state.usage,
        "record_completion",
        new_callable=AsyncMock,
    ) as meter:
        _, handlers = _build_capability(client, uid, sink)
        out = asyncio.run(
            handlers[GENERATE_IMAGE_TOOL_NAME](
                {"prompt": "a red bird", "model": "gpt-image-2"},
                ToolContext(),
            )
        )

    assert out == {"error": "Generated image could not be decoded."}
    assert sink == []
    meter.assert_awaited_once()
    assert meter.await_args.kwargs["status"] == "error"
    assert meter.await_args.kwargs["provider_completed"] is True


def test_post_provider_service_failure_is_metered_once_as_error(client):
    client.app.state.gateway.empty = True
    uid = _internal_id(client, {"X-Dev-User": "ian"})
    sink: list[MessageAttachment] = []
    with patch.object(
        client.app.state.usage,
        "record_completion",
        new_callable=AsyncMock,
    ) as meter:
        _, handlers = _build_capability(client, uid, sink)
        out = asyncio.run(
            handlers[GENERATE_IMAGE_TOOL_NAME](
                {"prompt": "a red bird", "model": "gpt-image-2"},
                ToolContext(),
            )
        )

    assert out == {"error": "Image generation returned no image."}
    assert sink == []
    meter.assert_awaited_once()
    assert meter.await_args.kwargs["status"] == "error"
    assert meter.await_args.kwargs["provider_completed"] is True


def test_pre_provider_service_failure_is_not_metered(client):
    client.app.state.gateway.error = ModelGatewayError(400, "request rejected")
    uid = _internal_id(client, {"X-Dev-User": "ian"})
    sink: list[MessageAttachment] = []
    with patch.object(
        client.app.state.usage,
        "record_completion",
        new_callable=AsyncMock,
    ) as meter:
        _, handlers = _build_capability(client, uid, sink)
        out = asyncio.run(
            handlers[GENERATE_IMAGE_TOOL_NAME](
                {"prompt": "a red bird", "model": "gpt-image-2"},
                ToolContext(),
            )
        )

    assert "error" in out
    assert sink == []
    meter.assert_not_awaited()


def test_blob_failure_is_metered_once_as_error(client):
    uid = _internal_id(client, {"X-Dev-User": "ian"})
    sink: list[MessageAttachment] = []
    with (
        patch.object(
            client.app.state.image_artifacts,
            "put",
            new_callable=AsyncMock,
            side_effect=RuntimeError("blob down"),
        ),
        patch.object(
            client.app.state.usage,
            "record_completion",
            new_callable=AsyncMock,
        ) as meter,
    ):
        _, handlers = _build_capability(client, uid, sink)
        out = asyncio.run(
            handlers[GENERATE_IMAGE_TOOL_NAME](
                {"prompt": "a red bird", "model": "gpt-image-2"},
                ToolContext(),
            )
        )

    assert out == {"error": "Generated image could not be stored."}
    assert sink == []
    meter.assert_awaited_once()
    assert meter.await_args.kwargs["status"] == "error"
    assert meter.await_args.kwargs["provider_completed"] is True


def test_blob_cancellation_is_metered_once_as_cancelled(client):
    uid = _internal_id(client, {"X-Dev-User": "ian"})
    sink: list[MessageAttachment] = []
    with (
        patch.object(
            client.app.state.image_artifacts,
            "put",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError,
        ),
        patch.object(
            client.app.state.usage,
            "record_completion",
            new_callable=AsyncMock,
        ) as meter,
    ):
        _, handlers = _build_capability(client, uid, sink)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                handlers[GENERATE_IMAGE_TOOL_NAME](
                    {"prompt": "a red bird", "model": "gpt-image-2"},
                    ToolContext(),
                )
            )

    assert sink == []
    meter.assert_awaited_once()
    assert meter.await_args.kwargs["status"] == "cancelled"
    assert meter.await_args.kwargs["provider_completed"] is True


def test_metering_cancellation_happens_after_attachment_commit(client):
    uid = _internal_id(client, {"X-Dev-User": "ian"})
    sink: list[MessageAttachment] = []
    with patch.object(
        client.app.state.usage,
        "record_completion",
        new_callable=AsyncMock,
        side_effect=asyncio.CancelledError,
    ) as meter:
        _, handlers = _build_capability(client, uid, sink)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                handlers[GENERATE_IMAGE_TOOL_NAME](
                    {"prompt": "a red bird", "model": "gpt-image-2"},
                    ToolContext(),
                )
            )

    assert len(sink) == 1
    meter.assert_awaited_once()
    assert meter.await_args.kwargs["status"] == "complete"
    assert meter.await_args.kwargs["provider_completed"] is True


def test_handler_result_has_no_payload_leak_even_when_artifact_id_contains_b64(client):
    """Regression: a CI run failed because a random `artifact_id` happened to
    contain "b64" as a substring (observed: "7db646..."), tripping a prior
    `"b64" not in str(out).lower()` check over the whole result dict.
    `uuid4().hex` is lowercase hex (``0-9a-f``), and "b64" is itself a valid
    3-hex-digit sequence, so it turns up by chance in roughly 1 of every ~140
    random ids - this was never a real payload leak. Pin the reproduction so
    the check can't silently regress to a whole-dict substring scan.
    """
    headers = {"X-Dev-User": "ian"}
    uid = _internal_id(client, headers)
    sink: list[MessageAttachment] = []
    _, handlers = _build_capability(client, uid, sink)
    handler = handlers[GENERATE_IMAGE_TOOL_NAME]

    colliding_id = uuid.UUID(hex="7db646aaaaaaaaaaaaaaaaaaaaaaaaaa")
    with patch("ai4ia_api.images.capability.uuid4", return_value=colliding_id):
        out = asyncio.run(
            handler({"prompt": "a red bird", "model": "gpt-image-2"}, ToolContext())
        )

    assert out["status"] == "generated"
    assert out["artifact_id"] == colliding_id.hex
    assert "b64" in out["artifact_id"]  # confirms this reproduces the collision
    # The precise, non-flaky check: no payload key, no leaked pixel value.
    forbidden_payload_keys = {"b64_json", "b64", "images_b64", "image_b64", "data"}
    assert not forbidden_payload_keys & out.keys()
    assert all(TINY_PNG_B64 not in str(value) for value in out.values())


def test_handler_blocks_disabled_user(client):
    headers = {"X-Dev-User": "banned"}
    uid = _internal_id(client, headers)
    client.put(
        f"/api/admin/entitlements/{uid}",
        json={"disabled": True},
        headers={"X-Dev-User": "alice"},
    )
    sink: list[MessageAttachment] = []
    _, handlers = _build_capability(client, uid, sink)
    out = asyncio.run(
        handlers[GENERATE_IMAGE_TOOL_NAME]({"prompt": "x"}, ToolContext())
    )
    assert "error" in out
    assert sink == []


def test_handler_enforces_per_turn_budget(client):
    headers = {"X-Dev-User": "ian"}
    uid = _internal_id(client, headers)
    sink: list[MessageAttachment] = []
    _, handlers = _build_capability(client, uid, sink)
    handler = handlers[GENERATE_IMAGE_TOOL_NAME]
    for _ in range(MAX_IMAGES_PER_TURN):
        ok = asyncio.run(handler({"prompt": "x", "model": "gpt-image-2"}, ToolContext()))
        assert ok["status"] == "generated"
    over = asyncio.run(handler({"prompt": "x", "model": "gpt-image-2"}, ToolContext()))
    assert "error" in over
    assert len(sink) == MAX_IMAGES_PER_TURN


# ---- serve endpoint ownership ----


def test_serve_endpoint_owner_can_read_others_cannot(client):
    owner = {"X-Dev-User": "owner"}
    other = {"X-Dev-User": "intruder"}
    owner_id = _internal_id(client, owner)
    sink: list[MessageAttachment] = []
    _, handlers = _build_capability(client, owner_id, sink)
    out = asyncio.run(
        handlers[GENERATE_IMAGE_TOOL_NAME](
            {"prompt": "x", "model": "gpt-image-2"}, ToolContext()
        )
    )
    artifact_id = out["artifact_id"]

    # Owner reads their own image bytes.
    r_owner = client.get(f"/api/images/artifacts/{artifact_id}", headers=owner)
    assert r_owner.status_code == 200
    assert r_owner.headers["content-type"] == "image/png"
    assert r_owner.content == base64.b64decode(TINY_PNG_B64)

    # A different user guessing the same id gets a 404, never a cross-user read.
    r_other = client.get(f"/api/images/artifacts/{artifact_id}", headers=other)
    assert r_other.status_code == 404


def test_serve_endpoint_rejects_malformed_id(client):
    r = client.get(
        "/api/images/artifacts/..%2f..%2fetc", headers={"X-Dev-User": "ian"}
    )
    assert r.status_code in (404, 400)


def test_artifact_id_pattern_matches_only_a_uuid4_hex_token():
    # Every real artifact id is uuid4().hex: exactly 32 lowercase hex chars.
    # A too-short or too-long hex run must not match, even though it would
    # still 404 (via blob-not-found) either way over HTTP.
    from ai4ia_api.routers.images import _ARTIFACT_ID_RE

    assert _ARTIFACT_ID_RE.match("a" * 32)
    assert not _ARTIFACT_ID_RE.match("a" * 31)
    assert not _ARTIFACT_ID_RE.match("a" * 33)
    assert not _ARTIFACT_ID_RE.match("a" * 8)


def test_serve_endpoint_unknown_id_404(client):
    r = client.get(
        "/api/images/artifacts/" + "a" * 32, headers={"X-Dev-User": "ian"}
    )
    assert r.status_code == 404


# ---- Message.attachments serialization ----


def test_message_attachments_round_trip():
    msg = Message(
        sessionId="s1",
        userId="u1",
        role=MessageRole.assistant,
        content="here it is",
        attachments=[
            MessageAttachment(
                id="a" * 32,
                kind="image",
                mimeType="image/png",
                prompt="a cat",
                model="gpt-image-2",
                size="1024x1024",
            )
        ],
    )
    doc = msg.model_dump(mode="json")
    assert doc["attachments"][0]["id"] == "a" * 32
    restored = Message.model_validate(doc)
    assert len(restored.attachments) == 1
    assert restored.attachments[0].model == "gpt-image-2"
    assert restored.attachments[0].size == "1024x1024"


def test_message_defaults_to_no_attachments():
    msg = Message(
        sessionId="s1", userId="u1", role=MessageRole.user, content="hi"
    )
    assert msg.attachments == []
