"""Library upload endpoint: default-OFF refusal, the stored-then-enrich
happy path, dedupe, caps (413/409/422), and analyzer validation. CU is not
configured in these settings, so enrichment is an inert background no-op and the
document settles at ``stored`` — exactly the local/default posture."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.library.ingest import EnrichScheduleOutcome
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


def _upload(client: TestClient, name: str, data: bytes, ctype: str = "text/plain", **form):
    return client.post(
        "/api/library/documents",
        files={"file": (name, data, ctype)},
        data=form,
    )


def test_upload_disabled_by_default_returns_404():
    app = create_app(make_settings())  # document_understanding_enabled defaults False
    with TestClient(app) as c:
        resp = c.post(
            "/api/library/documents",
            files={"file": ("a.txt", b"hi", "text/plain")},
        )
        assert resp.status_code == 404


def test_upload_happy_path_stored(client):
    resp = _upload(client, "note.txt", b"hello world content")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "stored"
    assert body["filename"] == "note.txt"
    assert body["summary"] == "hello world content"
    # It appears in the user's library listing.
    listed = client.get("/api/library/documents").json()
    assert body["id"] in {d["id"] for d in listed}


def test_upload_saturation_settles_failed_instead_of_stored_orphan(
    client, monkeypatch
):
    ingestor = client.app.state.document_ingestor
    ingestor._cu = object()
    monkeypatch.setattr(
        ingestor,
        "schedule_enrich",
        lambda **_kwargs: EnrichScheduleOutcome.saturated,
    )

    response = _upload(client, "busy.txt", b"retryable content")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert "Re-upload to retry" in body["error"]


def test_upload_dedupe_returns_same_document(client):
    first = _upload(client, "a.txt", b"identical bytes")
    second = _upload(client, "a.txt", b"identical bytes")
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/api/library/documents").json()) == 1


def test_upload_empty_file_422(client):
    resp = _upload(client, "empty.txt", b"")
    assert resp.status_code == 422


def test_upload_too_large_413():
    c = _client(document_max_upload_bytes=10)
    try:
        resp = _upload(c, "big.txt", b"x" * 50)
        assert resp.status_code == 413
    finally:
        c.__exit__(None, None, None)


def test_upload_per_user_cap_409():
    c = _client(document_max_per_user=1)
    try:
        assert _upload(c, "a.txt", b"first file").status_code == 201
        assert _upload(c, "b.txt", b"second file").status_code == 409
    finally:
        c.__exit__(None, None, None)


def test_upload_unknown_analyzer_404(client):
    resp = _upload(client, "a.txt", b"data", analyzerId="does-not-exist")
    assert resp.status_code == 404


def test_upload_builtin_analyzer_accepted(client):
    resp = _upload(client, "a.txt", b"data here", analyzerId="builtin-document")
    assert resp.status_code == 201
    assert resp.json()["analyzerId"] == "builtin-document"
