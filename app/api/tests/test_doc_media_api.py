"""Phase 11D media endpoints: GET /api/library/documents/{id}/media (original
audio/video byte stream for the deep-link player) and .../timeline (scene/keyframe
markers). Default-OFF refusal, the ready-AV happy paths, the non-AV refusal, and
cross-user isolation. All IO is in-memory; no network."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.auth.base import AuthCredentials
from ai4ia_api.library.blob_store import MEDIA_NAME, RAW_NAME, blob_path
from ai4ia_api.library.models import DocumentStatus, Modality, UserDocument
from ai4ia_api.main import create_app
from tests.conftest import make_settings


def _client(**overrides) -> TestClient:
    app = create_app(make_settings(document_understanding_enabled=True, **overrides))
    c = TestClient(app)
    c.__enter__()
    return c


@pytest.fixture
def client():
    c = _client()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _uid(client: TestClient, sub: str | None = None) -> str:
    headers = {"X-Dev-User": sub} if sub else {}
    provider = client.app.state.auth_provider
    user = asyncio.run(provider.authenticate(AuthCredentials(headers=headers)))
    return user.internal_user_id


async def _seed_media(
    client: TestClient,
    *,
    uid: str,
    status: DocumentStatus = DocumentStatus.ready,
    filename: str = "lecture.mp4",
    content_type: str = "video/mp4",
    modality: Modality = Modality.video,
    raw: bytes | None = b"VIDEOBYTES",
    timeline: dict | None = None,
) -> UserDocument:
    doc = UserDocument(
        userId=uid,
        filename=filename,
        status=status,
        summary="s",
        contentType=content_type,
        modality=modality,
    )
    blob = client.app.state.document_ingestor.blob
    if raw is not None:
        rpath = blob_path(uid, doc.id, RAW_NAME)
        await blob.put(rpath, raw, content_type)
        doc.rawPath = rpath
    if timeline is not None:
        mpath = blob_path(uid, doc.id, MEDIA_NAME)
        await blob.put(mpath, json.dumps(timeline).encode("utf-8"), "application/json")
    await client.app.state.document_library.create_document(doc)
    return doc


def test_media_endpoints_disabled_by_default_return_404():
    app = create_app(make_settings())  # document_understanding_enabled defaults False
    with TestClient(app) as c:
        assert c.get("/api/library/documents/anything/media").status_code == 404
        assert c.get("/api/library/documents/anything/timeline").status_code == 404


def test_media_stream_happy_path_returns_original_bytes(client):
    uid = _uid(client)
    doc = asyncio.run(_seed_media(client, uid=uid, raw=b"REALMP4BYTES"))

    resp = client.get(f"/api/library/documents/{doc.id}/media")
    assert resp.status_code == 200, resp.text
    assert resp.content == b"REALMP4BYTES"
    assert resp.headers["content-type"].startswith("video/mp4")
    assert "inline" in resp.headers["content-disposition"]


def test_media_stream_rejects_non_audiovisual(client):
    uid = _uid(client)
    doc = asyncio.run(
        _seed_media(
            client, uid=uid, filename="report.pdf",
            content_type="application/pdf", modality=Modality.document,
        )
    )
    assert client.get(f"/api/library/documents/{doc.id}/media").status_code == 404


def test_media_stream_cross_user_is_404(client):
    other = _uid(client, sub="someone-else")
    doc = asyncio.run(_seed_media(client, uid=other))
    # Default authed user is not the owner.
    assert client.get(f"/api/library/documents/{doc.id}/media").status_code == 404


def test_media_stream_non_ready_is_404(client):
    uid = _uid(client)
    doc = asyncio.run(_seed_media(client, uid=uid, status=DocumentStatus.analyzing))
    assert client.get(f"/api/library/documents/{doc.id}/media").status_code == 404


def test_timeline_happy_path_returns_segments(client):
    uid = _uid(client)
    tl = {
        "durationMs": 60000,
        "segments": [
            {"index": 0, "startMs": 0, "endMs": 60000,
             "keyframes": [0, 5000], "shots": [0, 30000]},
        ],
    }
    doc = asyncio.run(_seed_media(client, uid=uid, timeline=tl))

    resp = client.get(f"/api/library/documents/{doc.id}/timeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["documentId"] == doc.id
    assert body["modality"] == "video"
    assert body["durationMs"] == 60000
    assert len(body["segments"]) == 1
    assert body["segments"][0]["keyframes"] == [0, 5000]
    assert body["segments"][0]["shots"] == [0, 30000]


def test_timeline_missing_sidecar_is_empty_200(client):
    uid = _uid(client)
    doc = asyncio.run(_seed_media(client, uid=uid, timeline=None))

    resp = client.get(f"/api/library/documents/{doc.id}/timeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["durationMs"] is None
    assert body["segments"] == []


def test_timeline_rejects_non_audiovisual(client):
    uid = _uid(client)
    doc = asyncio.run(
        _seed_media(
            client, uid=uid, filename="report.pdf",
            content_type="application/pdf", modality=Modality.document,
        )
    )
    assert client.get(f"/api/library/documents/{doc.id}/timeline").status_code == 404
