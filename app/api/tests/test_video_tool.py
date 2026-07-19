"""The agent-callable ``generate_video`` capability + artifact serve endpoint.

Mirrors ``test_image_tool``: the synthetic tool's core contract (submit → poll →
download → persist → media-ref sink → meter), the per-user ownership boundary of
the authenticated serve endpoint, and that a generated-video reference round-trips
through ``Message`` serialization. A ``FakeVideoGateway`` stands in for Sora's
async job API so the poll loop is driven with a no-op sleep (no real delay).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.tool_exec import ToolContext
from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.main import create_app
from ai4ia_api.sessions.models import Message, MessageAttachment, MessageRole
from ai4ia_api.videos.capability import (
    GENERATE_VIDEO_TOOL_NAME,
    MAX_VIDEOS_PER_TURN,
    build_video_capability,
)
from ai4ia_api.videos.service import VideoGenerationError, VideoGenerationService
from tests.conftest import make_settings

# Tiny stand-in for MP4 bytes — the gateway is faked, so the content is opaque.
FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42fake-video-bytes"


class FakeVideoGateway:
    """Stand-in for Sora's async job API: create → poll → download."""

    def __init__(self) -> None:
        self.error: ModelGatewayError | None = None
        # By default the first poll already reports success.
        self.poll_statuses: list[dict] = [
            {"status": "succeeded", "generations": [{"id": "gen1"}]}
        ]
        self.content: bytes = FAKE_MP4
        self.calls: list[dict] = []

    async def create_video_job(
        self, *, deployment, prompt, width, height, n_seconds, correlation_id=None
    ):
        self.calls.append(
            {
                "deployment": deployment,
                "prompt": prompt,
                "width": width,
                "height": height,
                "n_seconds": n_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        return {"id": "job1", "status": "queued"}

    async def get_video_job(self, *, job_id, correlation_id=None):
        if len(self.poll_statuses) > 1:
            return self.poll_statuses.pop(0)
        return self.poll_statuses[0]

    async def get_video_content(self, *, generation_id, correlation_id=None):
        return self.content


async def _noop_sleep(_seconds: float) -> None:
    return None


def _client() -> TestClient:
    app = create_app(make_settings(admin_subjects="alice"))
    c = TestClient(app)
    c.__enter__()
    c.app.state.gateway = FakeVideoGateway()
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


def _service(client) -> VideoGenerationService:
    return VideoGenerationService(
        catalog=client.app.state.catalog,
        gateway=client.app.state.gateway,
        poll_interval_seconds=0.0,
        max_wait_seconds=1.0,
        sleep=_noop_sleep,
    )


def _build_capability(client, user_id: str, sink: list[MessageAttachment]):
    tools, handlers = build_video_capability(
        video_service=_service(client),
        artifact_store=client.app.state.video_artifacts,
        entitlements=client.app.state.entitlements,
        metering=client.app.state.usage,
        catalog=client.app.state.catalog,
        user_id=user_id,
        session_id="s-vid",
        sink=sink,
    )
    return tools, handlers


# ---- capability handler ----


def test_handler_generates_persists_sinks_and_meters(client):
    headers = {"X-Dev-User": "ian"}
    uid = _internal_id(client, headers)
    sink: list[MessageAttachment] = []
    tools, handlers = _build_capability(client, uid, sink)

    assert tools[0]["function"]["name"] == GENERATE_VIDEO_TOOL_NAME
    handler = handlers[GENERATE_VIDEO_TOOL_NAME]

    out = asyncio.run(
        handler(
            {"prompt": "a kite over the sea", "model": "sora-2", "seconds": 5},
            ToolContext(),
        )
    )
    assert out["status"] == "generated"
    artifact_id = out["artifact_id"]
    assert out["model"] == "sora-2"
    assert out["seconds"] == 5
    # The raw MP4 bytes never come back through the tool result.
    assert "ftyp" not in str(out)

    # A media reference was appended for the chat router to attach.
    assert len(sink) == 1
    att = sink[0]
    assert att.id == artifact_id
    assert att.kind == "video"
    assert att.mimeType == "video/mp4"
    assert att.prompt == "a kite over the sea"
    assert att.durationSeconds == 5

    # The bytes are durably stored, owner-scoped, and match the canned MP4.
    stored = asyncio.run(client.app.state.video_artifacts.get(uid, artifact_id))
    assert stored == FAKE_MP4

    # The call was metered into the usage ledger (rate/budget windows see it).
    summary = client.get("/api/usage", headers=headers).json()
    assert summary["totalRequests"] >= 1


def test_handler_defaults_size_and_seconds(client):
    headers = {"X-Dev-User": "ian"}
    uid = _internal_id(client, headers)
    sink: list[MessageAttachment] = []
    _, handlers = _build_capability(client, uid, sink)
    out = asyncio.run(
        handlers[GENERATE_VIDEO_TOOL_NAME]({"prompt": "a forest"}, ToolContext())
    )
    assert out["status"] == "generated"
    assert out["size"] == "1280x720"
    assert out["seconds"] == 5
    # Defaults flow to the gateway job submission.
    job = client.app.state.gateway.calls[0]
    assert job["width"] == 1280
    assert job["height"] == 720
    assert job["n_seconds"] == 5


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
        handlers[GENERATE_VIDEO_TOOL_NAME]({"prompt": "x"}, ToolContext())
    )
    assert "error" in out
    assert sink == []


def test_handler_enforces_per_turn_budget(client):
    headers = {"X-Dev-User": "ian"}
    uid = _internal_id(client, headers)
    sink: list[MessageAttachment] = []
    _, handlers = _build_capability(client, uid, sink)
    handler = handlers[GENERATE_VIDEO_TOOL_NAME]
    for _ in range(MAX_VIDEOS_PER_TURN):
        ok = asyncio.run(handler({"prompt": "x", "model": "sora-2"}, ToolContext()))
        assert ok["status"] == "generated"
    over = asyncio.run(handler({"prompt": "x", "model": "sora-2"}, ToolContext()))
    assert "error" in over
    assert len(sink) == MAX_VIDEOS_PER_TURN


def test_handler_surfaces_sanitized_upstream_error(client):
    headers = {"X-Dev-User": "ian"}
    uid = _internal_id(client, headers)
    client.app.state.gateway.error = ModelGatewayError(400, "content policy: nope")
    sink: list[MessageAttachment] = []
    _, handlers = _build_capability(client, uid, sink)
    out = asyncio.run(
        handlers[GENERATE_VIDEO_TOOL_NAME]({"prompt": "x", "model": "sora-2"}, ToolContext())
    )
    assert "error" in out
    assert sink == []


# ---- service-level orchestration ----


def test_service_times_out_when_job_never_succeeds(client):
    gateway = client.app.state.gateway
    gateway.poll_statuses = [{"status": "running"}]
    service = VideoGenerationService(
        catalog=client.app.state.catalog,
        gateway=gateway,
        poll_interval_seconds=0.0,
        max_wait_seconds=0.0,
        sleep=_noop_sleep,
    )
    with pytest.raises(VideoGenerationError) as ei:
        asyncio.run(service.generate(prompt="x", model="sora-2", size=None))
    assert ei.value.status_code == 504


def test_service_rejects_non_video_model(client):
    service = _service(client)
    with pytest.raises(VideoGenerationError) as ei:
        asyncio.run(service.generate(prompt="x", model="gpt-image-2", size=None))
    assert ei.value.status_code == 400


def test_service_rejects_bad_size(client):
    service = _service(client)
    with pytest.raises(VideoGenerationError) as ei:
        asyncio.run(service.generate(prompt="x", model="sora-2", size="999x999"))
    assert ei.value.status_code == 422


# ---- serve endpoint ownership ----


def test_serve_endpoint_owner_can_read_others_cannot(client):
    owner = {"X-Dev-User": "owner"}
    other = {"X-Dev-User": "intruder"}
    owner_id = _internal_id(client, owner)
    sink: list[MessageAttachment] = []
    _, handlers = _build_capability(client, owner_id, sink)
    out = asyncio.run(
        handlers[GENERATE_VIDEO_TOOL_NAME](
            {"prompt": "x", "model": "sora-2"}, ToolContext()
        )
    )
    artifact_id = out["artifact_id"]

    r_owner = client.get(f"/api/videos/artifacts/{artifact_id}", headers=owner)
    assert r_owner.status_code == 200
    assert r_owner.headers["content-type"] == "video/mp4"
    assert r_owner.content == FAKE_MP4

    r_other = client.get(f"/api/videos/artifacts/{artifact_id}", headers=other)
    assert r_other.status_code == 404


def test_serve_endpoint_rejects_malformed_id(client):
    r = client.get(
        "/api/videos/artifacts/..%2f..%2fetc", headers={"X-Dev-User": "ian"}
    )
    assert r.status_code in (404, 400)


def test_artifact_id_pattern_matches_only_a_uuid4_hex_token():
    # Every real artifact id is uuid4().hex: exactly 32 lowercase hex chars.
    # A too-short or too-long hex run must not match, even though it would
    # still 404 (via blob-not-found) either way over HTTP.
    from ai4ia_api.routers.videos import _ARTIFACT_ID_RE

    assert _ARTIFACT_ID_RE.match("a" * 32)
    assert not _ARTIFACT_ID_RE.match("a" * 31)
    assert not _ARTIFACT_ID_RE.match("a" * 33)
    assert not _ARTIFACT_ID_RE.match("a" * 8)


def test_serve_endpoint_unknown_id_404(client):
    r = client.get(
        "/api/videos/artifacts/" + "a" * 32, headers={"X-Dev-User": "ian"}
    )
    assert r.status_code == 404


# ---- Message.attachments serialization (video) ----


def test_video_attachment_round_trip():
    msg = Message(
        sessionId="s1",
        userId="u1",
        role=MessageRole.assistant,
        content="here it is",
        attachments=[
            MessageAttachment(
                id="b" * 32,
                kind="video",
                mimeType="video/mp4",
                prompt="a kite",
                model="sora-2",
                size="1280x720",
                durationSeconds=5,
            )
        ],
    )
    doc = msg.model_dump(mode="json")
    assert doc["attachments"][0]["kind"] == "video"
    restored = Message.model_validate(doc)
    assert len(restored.attachments) == 1
    assert restored.attachments[0].model == "sora-2"
    assert restored.attachments[0].durationSeconds == 5
