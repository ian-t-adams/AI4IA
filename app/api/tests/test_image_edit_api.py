"""``POST /api/images/edits``: a governed, owner-scoped, user-initiated edit.

Every case drives the real app, availability predicate, source resolver, mask
builder, metering and persistence. Only the provider is a recording fake, so each
test can prove exactly what would (or would not) have left the process. Refusals
are paired with the same request succeeding once the one condition under test is
corrected.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import zlib
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.library.blob_store import RAW_NAME, InMemoryBlobStore, blob_path
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.library.models import DocumentStatus, Modality, UserDocument, Visibility
from ai4ia_api.library.retrieval import DocumentRetrievalService
from ai4ia_api.main import create_app
from ai4ia_api.sessions import models as session_models
from tests.conftest import make_settings
from tests.image_edit_fixtures import (
    EDITED_PNG,
    SOURCE_PNG,
    SUNBURST,
    FakeEditGateway,
    editing_model_ids,
    image_edit_catalog_path,
    png_bytes,
    seed_generated_image,
)
from tests.test_image_edit_source import jpeg

ARTIFACT = "a" * 32
OWNER = {"X-Dev-User": "owner"}
INTRUDER = {"X-Dev-User": "intruder"}


def _app(tmp_path, *, editing=True, disabled=(), overrides=None, **extra):
    return create_app(make_settings(
        image_generation_enabled=True, image_editing_enabled=editing,
        model_catalog_path=image_edit_catalog_path(
            tmp_path, disabled=disabled, overrides=overrides,
        ),
        **extra,
    ))


@pytest.fixture
def edit_client(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        client.app.state.gateway = FakeEditGateway()
        yield client


def _session(client: TestClient, headers=OWNER, **body) -> tuple[str, str]:
    created = client.post("/api/sessions", json={"model": "gpt-5.2", **body}, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()["userId"], created.json()["id"]


def _seeded(client: TestClient, headers=OWNER, artifact=ARTIFACT, data=SOURCE_PNG):
    user_id, session_id = _session(client, headers)
    asyncio.run(seed_generated_image(client.app, user_id, session_id, artifact, data))
    return user_id, session_id


def _edit(client, session_id, headers=OWNER, **body):
    payload = {
        "sessionId": session_id,
        "source": {"kind": "generated", "id": ARTIFACT},
        "prompt": "make the sky purple",
        **body,
    }
    return client.post("/api/images/edits", json=payload, headers=headers)


def _usage(client, headers=OWNER) -> dict:
    return client.get("/api/usage", headers=headers).json()


def _rgba_alpha(mask: bytes) -> tuple[int, int, list[bytes]]:
    position, idat = 8, b""
    width = height = 0
    while position < len(mask):
        (length,) = struct.unpack(">I", mask[position:position + 4])
        kind = mask[position + 4:position + 8]
        body = mask[position + 8:position + 8 + length]
        if kind == b"IHDR":
            width, height = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        position += 12 + length
    raw = zlib.decompress(idat)
    stride = 1 + width * 4
    return width, height, [raw[i * stride + 1:(i + 1) * stride] for i in range(height)]


# --- the successful direct edit ---------------------------------------------------


def test_edit_persists_messages_new_artifact_receipt_and_metering(edit_client):
    user_id, session_id = _seeded(edit_client)
    response = _edit(edit_client, session_id)
    assert response.status_code == 200, response.text
    gateway = edit_client.app.state.gateway
    (call,) = gateway.edit_calls
    # The provider received exactly the owned source bytes, whole-image (no mask).
    assert call["image"] == SOURCE_PNG
    assert call["image_content_type"] == "image/png"
    assert call["mask"] is None
    assert call["prompt"] == "make the sky purple"
    assert (call["size"], call["quality"], call["n"], call["api"]) == (None, None, 1, "chat")
    # Sunburst is the preferred default editing model.
    assert call["deployment"].startswith(f"{SUNBURST}-")

    user_message, assistant = response.json()["messages"]
    assert user_message["role"] == "user"
    assert user_message["content"] == "Edit image: make the sky purple"
    (attachment,) = assistant["attachments"]
    assert attachment["kind"] == "image"
    assert attachment["id"] != ARTIFACT
    assert (attachment["sourceKind"], attachment["sourceId"], attachment["masked"]) == (
        "generated", ARTIFACT, False,
    )
    assert attachment["model"] == SUNBURST
    assert attachment["costKnown"] is False and attachment["estimatedCostUsd"] is None
    served = edit_client.get(f"/api/images/artifacts/{attachment['id']}", headers=OWNER)
    assert served.status_code == 200 and served.content == EDITED_PNG
    assert edit_client.get(
        f"/api/images/artifacts/{attachment['id']}", headers=INTRUDER,
    ).status_code == 404

    history = edit_client.get(f"/api/sessions/{session_id}/messages", headers=OWNER).json()
    assert [m["id"] for m in history[-2:]] == [user_message["id"], assistant["id"]]

    receipt = assistant["executionReceipt"]
    assert receipt["runtime"]["modelId"] == SUNBURST
    assert receipt["runtime"]["api"] == "images/edits"
    assert "user_initiated_image_edit" in receipt["notes"]
    (tool_call,) = receipt["toolCalls"]
    assert tool_call["tool"] == "edit_image" and tool_call["outcome"] == "result"
    arguments = json.loads(tool_call["arguments"]["text"])
    assert arguments["source"] == {
        "kind": "generated",
        "sha256Prefix": hashlib.sha256(SOURCE_PNG).hexdigest()[:16],
        "bytes": len(SOURCE_PNG), "contentType": "image/png", "width": 40, "height": 30,
    }
    assert arguments["region"] is None
    assert json.loads(tool_call["result"]["text"])["model"] == SUNBURST
    serialized = json.dumps(receipt)
    for payload in (SOURCE_PNG, EDITED_PNG):
        assert base64.b64encode(payload).decode()[:24] not in serialized

    usage = _usage(edit_client)
    assert usage["totalRequests"] == 1 and usage["billableRequests"] == 1
    assert usage["totalTokens"] == 220


def test_an_edited_image_can_itself_be_edited(edit_client):
    _, session_id = _seeded(edit_client)
    first = _edit(edit_client, session_id).json()["messages"][1]["attachments"][0]
    second = _edit(edit_client, session_id, source={"kind": "generated", "id": first["id"]})
    assert second.status_code == 200, second.text
    assert edit_client.app.state.gateway.edit_calls[-1]["image"] == EDITED_PNG


def test_explicit_model_and_controls_are_forwarded(edit_client):
    _, session_id = _seeded(edit_client)
    response = _edit(
        edit_client, session_id, model="gpt-image-2", size="1536x1024", quality="high",
    )
    assert response.status_code == 200, response.text
    call = edit_client.app.state.gateway.edit_calls[-1]
    assert call["deployment"].startswith("gpt-image-2-")
    assert (call["size"], call["quality"]) == ("1536x1024", "high")


def test_options_advertise_exactly_the_edit_controls_the_endpoint_accepts(edit_client):
    # The generation picker narrows these rows' unset lists to one square size; the
    # edit dialog reads these separate fields, so a non-square source keeps "auto".
    options = edit_client.get("/api/images/options", headers=OWNER).json()
    models = {model["id"]: model for model in options["models"]}
    _, session_id = _seeded(edit_client)
    for model_id in editing_model_ids():
        model = models[model_id]
        assert model["editSizes"] == ["auto", "1024x1024", "1024x1536", "1536x1024"], model_id
        assert model["editQualities"] == ["auto", "low", "medium", "high"], model_id
        for size in model["editSizes"]:
            accepted = _edit(edit_client, session_id, model=model_id, size=size)
            assert accepted.status_code == 200, (model_id, size, accepted.text)
        for quality in model["editQualities"]:
            accepted = _edit(edit_client, session_id, model=model_id, quality=quality)
            assert accepted.status_code == 200, (model_id, quality, accepted.text)
        # Paired control: a size the list omits is refused in the same place.
        assert _edit(edit_client, session_id, model=model_id, size="512x512").status_code == 422
    for model in options["models"]:
        if not model["editing"]:
            assert model["editSizes"] is None and model["editQualities"] is None, model["id"]


def test_omitted_controls_default_to_auto_or_the_first_declared_value(tmp_path):
    narrow = {"gpt-image-2": {"imageSizes": ["1536x1024"], "imageQualities": ["high"]}}
    with TestClient(_app(tmp_path, overrides=narrow)) as client:
        client.app.state.gateway = FakeEditGateway()
        models = {
            m["id"]: m for m in client.get("/api/images/options", headers=OWNER).json()["models"]
        }
        assert (models["gpt-image-2"]["editSizes"], models["gpt-image-2"]["editQualities"]) == (
            ["1536x1024"], ["high"],
        )
        _, session_id = _seeded(client)
        # A row that declares no "auto" gets its first declared value, not a 422.
        assert _edit(client, session_id, model="gpt-image-2").status_code == 200
        call = client.app.state.gateway.edit_calls[-1]
        assert (call["size"], call["quality"]) == ("1536x1024", "high")
        # Paired control: a row with unset lists still sends "auto" (omitted upstream).
        assert _edit(client, session_id, model=SUNBURST).status_code == 200
        call = client.app.state.gateway.edit_calls[-1]
        assert (call["size"], call["quality"]) == (None, None)


def test_the_reply_is_stamped_in_a_later_millisecond_than_the_request(edit_client, monkeypatch):
    # Browsers sort a transcript by millisecond timestamps. Freeze the message clock
    # so both messages would otherwise share one instant, in a future millisecond.
    frozen = datetime(2100, 1, 1, 0, 0, 0, 400, tzinfo=timezone.utc)

    class _FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    _, session_id = _seeded(edit_client)
    monkeypatch.setattr(session_models, "datetime", _FrozenClock)
    response = _edit(edit_client, session_id)
    monkeypatch.undo()
    assert response.status_code == 200, response.text
    user_message, assistant = response.json()["messages"]

    def millisecond(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(microsecond=parsed.microsecond // 1000 * 1000)

    assert datetime.fromisoformat(user_message["createdAt"].replace("Z", "+00:00")) == frozen
    assert millisecond(assistant["createdAt"]) > millisecond(user_message["createdAt"])
    history = edit_client.get(f"/api/sessions/{session_id}/messages", headers=OWNER).json()
    assert [m["id"] for m in history[-2:]] == [user_message["id"], assistant["id"]]


# --- models: capability, runtime enablement and the default -----------------------


@pytest.mark.parametrize("model", ["FLUX.2-pro", "MAI-Image-2.6", "gpt-5.2", "not-a-model"])
def test_non_editing_models_are_refused_before_any_provider_call(edit_client, model):
    _, session_id = _seeded(edit_client)
    refused = _edit(edit_client, session_id, model=model)
    assert refused.status_code == 400, refused.text
    assert edit_client.app.state.gateway.edit_calls == []
    assert _usage(edit_client)["totalRequests"] == 0
    # Paired control: every documented editing model is accepted in the same place.
    for editing in editing_model_ids():
        assert _edit(edit_client, session_id, model=editing).status_code == 200, editing
    assert len(edit_client.app.state.gateway.edit_calls) == len(editing_model_ids())


@pytest.mark.parametrize("runtime_enabled", [True, False])
def test_a_runtime_disabled_editing_model_is_refused_without_substitution(tmp_path, runtime_enabled):
    app = _app(tmp_path, disabled=() if runtime_enabled else ("gpt-image-2",))
    with TestClient(app) as client:
        client.app.state.gateway = FakeEditGateway()
        _, session_id = _seeded(client)
        response = _edit(client, session_id, model="gpt-image-2")
        calls = client.app.state.gateway.edit_calls
        if runtime_enabled:
            assert response.status_code == 200, response.text
            assert calls[0]["deployment"].startswith("gpt-image-2-")
        else:
            assert response.status_code == 400
            assert response.json()["detail"] == "Unknown or unavailable model: gpt-image-2"
            assert calls == []


def test_default_falls_back_when_sunburst_is_unavailable(tmp_path):
    app = _app(tmp_path, disabled=(SUNBURST,))
    with TestClient(app) as client:
        client.app.state.gateway = FakeEditGateway()
        options = client.get("/api/images/options", headers=OWNER).json()
        assert options["editingEnabled"] is True
        fallback = options["defaultEditModel"]
        assert fallback in editing_model_ids() and fallback != SUNBURST
        _, session_id = _seeded(client)
        assert _edit(client, session_id).status_code == 200
        assert client.app.state.gateway.edit_calls[0]["deployment"].startswith(f"{fallback}-")


def test_no_runtime_editing_model_hides_editing_and_refuses(tmp_path):
    app = _app(tmp_path, disabled=tuple(editing_model_ids()))
    with TestClient(app) as client:
        client.app.state.gateway = FakeEditGateway()
        options = client.get("/api/images/options", headers=OWNER).json()
        assert options["enabled"] is True
        assert options["editingEnabled"] is False and options["defaultEditModel"] is None
        _, session_id = _seeded(client)
        refused = _edit(client, session_id)
        assert refused.status_code == 404
        assert refused.json()["detail"] == "No runtime-enabled image editing model is available."
        assert client.app.state.gateway.edit_calls == []


@pytest.mark.parametrize("editing", [True, False])
def test_the_flag_gates_options_endpoint_and_library_preview(tmp_path, editing):
    app = _app(tmp_path, editing=editing)
    with TestClient(app) as client:
        client.app.state.gateway = FakeEditGateway()
        options = client.get("/api/images/options", headers=OWNER).json()
        assert options["editingEnabled"] is editing
        assert options["defaultEditModel"] == (SUNBURST if editing else None)
        flagged = {m["id"] for m in options["models"] if m["editing"]}
        assert flagged == set(editing_model_ids())
        _, session_id = _seeded(client)
        response = _edit(client, session_id)
        assert response.status_code == (200 if editing else 404), response.text
        assert len(client.app.state.gateway.edit_calls) == (1 if editing else 0)
        library, blob = _library(client)
        user_id = client.get("/api/entitlement", headers=OWNER).json()["userId"]
        doc = _library_image(library, blob, user_id)
        preview = client.get(f"/api/images/sources/library/{doc.id}", headers=OWNER)
        assert preview.status_code == (200 if editing else 404)


# --- ownership and conversation scoping ---------------------------------------------


def test_another_owners_conversation_or_artifact_is_never_read(edit_client):
    _, session_id = _seeded(edit_client)
    # The intruder names the owner's conversation: 404 before any source read.
    assert _edit(edit_client, session_id, headers=INTRUDER).status_code == 404
    # The intruder names the owner's artifact id inside their own conversation.
    _, intruder_session = _session(edit_client, INTRUDER)
    assert _edit(edit_client, intruder_session, headers=INTRUDER).status_code == 404
    assert edit_client.app.state.gateway.edit_calls == []
    # Paired control: the owner's identical request succeeds.
    assert _edit(edit_client, session_id).status_code == 200


def test_an_artifact_from_another_of_the_owners_conversations_is_refused(edit_client):
    user_id, first = _seeded(edit_client)
    _, second = _session(edit_client)
    refused = _edit(edit_client, second)
    assert refused.status_code == 404
    assert refused.json()["detail"] == "That image is not part of this conversation."
    assert edit_client.app.state.gateway.edit_calls == []
    assert _edit(edit_client, first).status_code == 200


def test_failed_images_and_malformed_ids_are_not_sources(edit_client):
    user_id, session_id = _session(edit_client)
    asyncio.run(seed_generated_image(
        edit_client.app, user_id, session_id, ARTIFACT, kind="image_error",
    ))
    assert _edit(edit_client, session_id).status_code == 404
    assert _edit(
        edit_client, session_id, source={"kind": "generated", "id": "../../x"},
    ).status_code == 404
    assert edit_client.app.state.gateway.edit_calls == []


@pytest.mark.parametrize(
    ("data", "status"),
    [
        (b"GIF89a" + b"\x00" * 64, 422),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24, 422),
        (SOURCE_PNG[:-12], 422),
    ],
    ids=["gif", "webp", "truncated-png"],
)
def test_unsupported_or_corrupt_source_bytes_are_refused(edit_client, data, status):
    _, session_id = _seeded(edit_client, data=data)
    refused = _edit(edit_client, session_id)
    assert refused.status_code == status, refused.text
    assert edit_client.app.state.gateway.edit_calls == []


def test_jpeg_sources_keep_their_type(edit_client):
    _, session_id = _seeded(edit_client, data=jpeg(640, 480))
    assert _edit(edit_client, session_id).status_code == 200
    assert edit_client.app.state.gateway.edit_calls[0]["image_content_type"] == "image/jpeg"


# --- region masks ---------------------------------------------------------------------


def test_a_region_becomes_an_exact_alpha_mask(edit_client):
    _, session_id = _seeded(edit_client)
    region = {"x": 0.25, "y": 0.5, "width": 0.5, "height": 0.25}
    response = _edit(edit_client, session_id, region=region)
    assert response.status_code == 200, response.text
    mask = edit_client.app.state.gateway.edit_calls[0]["mask"]
    width, height, rows = _rgba_alpha(mask)
    assert (width, height) == (40, 30)
    for y, row in enumerate(rows):
        for x in range(width):
            inside = 10 <= x < 30 and 15 <= y < 23
            assert row[x * 4 + 3] == (0 if inside else 255)
    user_message, assistant = response.json()["messages"]
    assert user_message["content"] == "Edit image (selected region): make the sky purple"
    assert assistant["attachments"][0]["masked"] is True
    receipt_args = json.loads(assistant["executionReceipt"]["toolCalls"][0]["arguments"]["text"])
    assert receipt_args["region"] == region


@pytest.mark.parametrize(
    "region",
    [
        {"x": 0.6, "y": 0, "width": 0.5, "height": 0.5},
        {"x": 0, "y": 0, "width": 0, "height": 0.5},
        {"x": -0.1, "y": 0, "width": 0.5, "height": 0.5},
        {"x": "0.1", "y": 0, "width": 0.5, "height": 0.5},
        {"x": True, "y": 0, "width": 0.5, "height": 0.5},
        {"x": 0, "y": 0, "width": 0.5},
        {"x": 0, "y": 0, "width": 0.5, "height": 0.5, "extra": 1},
    ],
    ids=["past-edge", "zero-width", "negative", "string", "bool", "missing", "extra-field"],
)
def test_invalid_regions_are_refused_before_any_provider_call(edit_client, region):
    _, session_id = _seeded(edit_client)
    refused = _edit(edit_client, session_id, region=region)
    assert refused.status_code == 422, refused.text
    assert edit_client.app.state.gateway.edit_calls == []
    ok = _edit(edit_client, session_id, region={"x": 0, "y": 0, "width": 0.5, "height": 0.5})
    assert ok.status_code == 200


def test_region_edits_refuse_a_rotated_photo_but_whole_image_edits_proceed(edit_client):
    rotated = jpeg(640, 480, orientation=6)
    _, session_id = _seeded(edit_client, data=rotated)
    refused = _edit(edit_client, session_id, region={"x": 0, "y": 0, "width": 0.5, "height": 0.5})
    assert refused.status_code == 422
    assert "rotated photo" in refused.json()["detail"]
    assert edit_client.app.state.gateway.edit_calls == []
    assert _edit(edit_client, session_id).status_code == 200
    # Paired control: the same region on an upright JPEG builds a mask and proceeds.
    _, upright = _seeded(edit_client, artifact="b" * 32, data=jpeg(640, 480, orientation=1))
    ok = _edit(
        edit_client, upright, source={"kind": "generated", "id": "b" * 32},
        region={"x": 0, "y": 0, "width": 0.5, "height": 0.5},
    )
    assert ok.status_code == 200, ok.text
    assert edit_client.app.state.gateway.edit_calls[-1]["mask"] is not None


# --- entitlement, provider errors and metering -------------------------------------


def test_entitlement_denial_reaches_no_provider(edit_client):
    from ai4ia_api.entitlements.models import EntitlementDecision

    _, session_id = _seeded(edit_client)

    class Deny:
        async def check(self, user_id):
            return EntitlementDecision(allowed=False, code=403, reason="Account disabled.")

    real = edit_client.app.state.entitlements
    edit_client.app.state.entitlements = Deny()
    try:
        refused = _edit(edit_client, session_id)
        assert refused.status_code == 403
        assert refused.json()["detail"] == "Account disabled."
        assert edit_client.app.state.gateway.edit_calls == []
    finally:
        edit_client.app.state.entitlements = real
    assert _edit(edit_client, session_id).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"code": "contentFilter", "message": "Your task failed as a result of our safety system."}},
        {"error": {"code": "invalid_request", "innererror": {"code": "ResponsibleAIPolicyViolation"}}},
        {"error": {"code": "content_policy_violation", "message": "blocked"}},
        {"error": {"code": "moderation_blocked", "message": "blocked"}},
    ],
    ids=["contentFilter", "inner-rai", "content_policy_violation", "moderation_blocked"],
)
def test_content_filtering_is_sanitized_and_not_metered(edit_client, body):
    _, session_id = _seeded(edit_client)
    edit_client.app.state.gateway.error = ModelGatewayError(400, json.dumps(body))
    refused = _edit(edit_client, session_id)
    assert refused.status_code == 400
    assert refused.json()["detail"] == (
        "The edit was blocked by the content safety system. Try a different image or prompt."
    )
    assert _usage(edit_client)["totalRequests"] == 0
    history = edit_client.get(f"/api/sessions/{session_id}/messages", headers=OWNER).json()
    assert len(history) == 1


@pytest.mark.parametrize(
    ("status_code", "expected_status", "detail"),
    [
        (400, 400, "size is not supported"),
        (401, 502, "Image provider rejected the request."),
        (429, 429, "Image provider is rate limited. Try again shortly."),
        (500, 502, "Image edit failed."),
        (404, 502, "Image edit failed."),
    ],
)
def test_provider_errors_are_sanitized(edit_client, status_code, expected_status, detail):
    _, session_id = _seeded(edit_client)
    edit_client.app.state.gateway.error = ModelGatewayError(
        status_code, json.dumps({"error": {"code": "bad", "message": "size is not supported"}}),
    )
    refused = _edit(edit_client, session_id)
    assert refused.status_code == expected_status
    assert refused.json()["detail"] == detail
    if status_code == 429:
        assert refused.headers["retry-after"] == "30"


def test_provider_completed_empty_output_is_metered_once_as_error(edit_client):
    _, session_id = _seeded(edit_client)
    edit_client.app.state.gateway.reply = {"data": [], "usage": {"total_tokens": 9}}
    refused = _edit(edit_client, session_id)
    assert refused.status_code == 502
    assert refused.json()["detail"] == "Image edit returned no image."
    usage = _usage(edit_client)
    assert usage["totalRequests"] == 1 and usage["billableRequests"] == 1


def test_request_shape_is_strict(edit_client):
    _, session_id = _seeded(edit_client)
    for bad in (
        {"prompt": ""},
        {"prompt": "x" * 4001},
        {"source": {"kind": "url", "id": "https://example.com/a.png"}},
        {"source": {"kind": "generated", "id": ARTIFACT, "url": "https://example.com"}},
        {"imageUrl": "https://example.com/a.png"},
        {"size": "9999x9999"},
        {"quality": "max"},
    ):
        response = _edit(edit_client, session_id, **bad)
        assert response.status_code == 422, (bad, response.text)
    assert edit_client.app.state.gateway.edit_calls == []


# --- library sources ----------------------------------------------------------------


def _library(client: TestClient):
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    client.app.state.document_library = library
    client.app.state.document_retrieval = DocumentRetrievalService(
        library=library, blob_store=blob, chunk_store=None, embedder=None,
        settings=client.app.state.settings,
    )
    return library, blob


def _library_image(library, blob, user_id, *, data=SOURCE_PNG, filename="photo.png",
                   content_type="image/png", status=DocumentStatus.ready,
                   modality=Modality.image, visibility=Visibility.private, acl=None):
    doc = UserDocument(
        userId=user_id, filename=filename, contentType=content_type, size=len(data),
        status=status, modality=modality, visibility=visibility, acl=acl or [],
    )
    raw_path = blob_path(user_id, doc.id, f"{RAW_NAME}.png")
    doc.rawPath = raw_path

    async def seed():
        await blob.put(raw_path, data, content_type)
        await library.create_document(doc)

    asyncio.run(seed())
    return doc


def test_an_owned_library_image_in_scope_is_edited_byte_exactly(edit_client):
    library, blob = _library(edit_client)
    user_id = edit_client.get("/api/entitlement", headers=OWNER).json()["userId"]
    photo = png_bytes(64, 48, seed=0x33)
    doc = _library_image(library, blob, user_id, data=photo)
    _, session_id = _session(edit_client)  # libraryDocumentIds None: all owned docs
    preview = edit_client.get(f"/api/images/sources/library/{doc.id}", headers=OWNER)
    assert preview.status_code == 200
    assert preview.content == photo and preview.headers["content-type"] == "image/png"
    assert preview.headers["cache-control"] == "private, no-store"
    response = _edit(edit_client, session_id, source={"kind": "library", "id": doc.id})
    assert response.status_code == 200, response.text
    assert edit_client.app.state.gateway.edit_calls[0]["image"] == photo
    user_message, assistant = response.json()["messages"]
    assert user_message["content"] == "Edit image “photo.png”: make the sky purple"
    attachment = assistant["attachments"][0]
    assert (attachment["sourceKind"], attachment["sourceId"], attachment["filename"]) == (
        "library", doc.id, "photo.png",
    )


def test_library_scope_is_enforced(edit_client):
    library, blob = _library(edit_client)
    user_id = edit_client.get("/api/entitlement", headers=OWNER).json()["userId"]
    doc = _library_image(library, blob, user_id)
    other = _library_image(library, blob, user_id, filename="other.png")
    _, selected = _session(edit_client, libraryDocumentIds=[other.id])
    _, opted_out = _session(edit_client, libraryDocumentIds=[])
    for session_id in (selected, opted_out):
        refused = _edit(edit_client, session_id, source={"kind": "library", "id": doc.id})
        assert refused.status_code == 404
        assert "not selected for this conversation" in refused.json()["detail"]
    assert edit_client.app.state.gateway.edit_calls == []
    _, included = _session(edit_client, libraryDocumentIds=[doc.id])
    assert _edit(
        edit_client, included, source={"kind": "library", "id": doc.id},
    ).status_code == 200


def test_shared_and_public_library_images_are_not_editable_sources(edit_client):
    library, blob = _library(edit_client)
    owner_id = edit_client.get("/api/entitlement", headers=OWNER).json()["userId"]
    shared = _library_image(
        library, blob, owner_id, visibility=Visibility.shared, acl=["intruder@example.com"],
    )
    public = _library_image(library, blob, owner_id, visibility=Visibility.public)
    _, intruder_session = _session(edit_client, INTRUDER)
    for doc in (shared, public):
        refused = _edit(
            edit_client, intruder_session, headers=INTRUDER,
            source={"kind": "library", "id": doc.id},
        )
        assert refused.status_code == 404
        assert edit_client.get(
            f"/api/images/sources/library/{doc.id}", headers=INTRUDER,
        ).status_code == 404
    assert edit_client.app.state.gateway.edit_calls == []
    _, owner_session = _session(edit_client)
    assert _edit(
        edit_client, owner_session, source={"kind": "library", "id": shared.id},
    ).status_code == 200


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"status": DocumentStatus.analyzing}, 409),
        ({"modality": Modality.document, "content_type": "application/pdf"}, 422),
        ({"data": b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24,
          "content_type": "image/webp"}, 422),
        ({"data": b"GIF89a" + b"\x00" * 64, "content_type": "image/gif"}, 422),
    ],
    ids=["not-ready", "not-image", "webp", "gif"],
)
def test_ineligible_library_documents_are_refused(edit_client, changes, status):
    library, blob = _library(edit_client)
    user_id = edit_client.get("/api/entitlement", headers=OWNER).json()["userId"]
    doc = _library_image(library, blob, user_id, **changes)
    _, session_id = _session(edit_client)
    refused = _edit(edit_client, session_id, source={"kind": "library", "id": doc.id})
    assert refused.status_code == status, refused.text
    assert edit_client.app.state.gateway.edit_calls == []
    good = _library_image(library, blob, user_id)
    assert _edit(
        edit_client, session_id, source={"kind": "library", "id": good.id},
    ).status_code == 200


def test_an_oversize_library_image_is_refused_before_its_blob_is_read(edit_client):
    library, blob = _library(edit_client)
    user_id = edit_client.get("/api/entitlement", headers=OWNER).json()["userId"]
    doc = _library_image(library, blob, user_id)
    doc.size = 20_000_001
    asyncio.run(library.create_document(doc))
    reads: list[str] = []
    original = blob.get

    async def counting_get(path):
        reads.append(path)
        return await original(path)

    blob.get = counting_get  # type: ignore[method-assign]
    _, session_id = _session(edit_client)
    refused = _edit(edit_client, session_id, source={"kind": "library", "id": doc.id})
    assert refused.status_code == 413
    assert reads == []
    assert edit_client.app.state.gateway.edit_calls == []


def test_library_sources_require_the_library(edit_client):
    _, session_id = _session(edit_client)
    refused = _edit(edit_client, session_id, source={"kind": "library", "id": "doc-1"})
    assert refused.status_code == 404
    assert refused.json()["detail"] == "The document library is not available."


def test_exif_rotation_is_read_from_library_jpeg(edit_client):
    library, blob = _library(edit_client)
    user_id = edit_client.get("/api/entitlement", headers=OWNER).json()["userId"]
    doc = _library_image(
        library, blob, user_id, data=jpeg(640, 480, orientation=6), content_type="image/jpeg",
        filename="phone.jpg",
    )
    _, session_id = _session(edit_client)
    refused = _edit(
        edit_client, session_id, source={"kind": "library", "id": doc.id},
        region={"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5},
    )
    assert refused.status_code == 422
    whole = _edit(edit_client, session_id, source={"kind": "library", "id": doc.id})
    assert whole.status_code == 200
    assert edit_client.app.state.gateway.edit_calls[0]["image_content_type"] == "image/jpeg"
