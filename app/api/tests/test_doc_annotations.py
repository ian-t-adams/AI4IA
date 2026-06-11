"""CRUD for /api/library/documents/{id}/annotations (Phase 11E-2).

Annotations are owner-private notes pinned to a library document. These tests
exercise the flag gate, ownership gate, body sanitization/validation, and the
full create/list/update/delete lifecycle. All IO is in-memory; no network."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.auth.base import AuthCredentials
from ai4ia_api.library.models import DocumentStatus, UserDocument
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


async def _seed(
    client: TestClient,
    *,
    uid: str,
    status: DocumentStatus = DocumentStatus.ready,
) -> UserDocument:
    doc = UserDocument(userId=uid, filename="report.pdf", status=status)
    await client.app.state.document_library.create_document(doc)
    return doc


def test_annotations_404_when_library_disabled():
    c = TestClient(create_app(make_settings()))  # document understanding OFF
    c.__enter__()
    try:
        assert c.get("/api/library/documents/whatever/annotations").status_code == 404
        assert (
            c.post(
                "/api/library/documents/whatever/annotations",
                json={"body": "note"},
            ).status_code
            == 404
        )
    finally:
        c.__exit__(None, None, None)


def test_create_then_list_annotation(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))

    resp = client.post(
        f"/api/library/documents/{doc.id}/annotations",
        json={"body": "  Check the Q3 totals  ", "anchor": "page 4"},
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["body"] == "Check the Q3 totals"  # trimmed
    assert created["anchor"] == "page 4"
    assert created["id"]

    listed = client.get(f"/api/library/documents/{doc.id}/annotations")
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    assert items[0]["id"] == created["id"]


def test_create_annotation_works_on_non_ready_document(client):
    # Notes are metadata, not retrieval — you can annotate a doc that is still stored.
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid, status=DocumentStatus.stored))
    resp = client.post(
        f"/api/library/documents/{doc.id}/annotations", json={"body": "draft note"}
    )
    assert resp.status_code == 201, resp.text


def test_create_annotation_empty_body_is_422(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))
    # Pydantic min_length rejects "".
    assert (
        client.post(
            f"/api/library/documents/{doc.id}/annotations", json={"body": ""}
        ).status_code
        == 422
    )
    # Whitespace/control-only survives min_length but sanitizes to empty -> 422.
    assert (
        client.post(
            f"/api/library/documents/{doc.id}/annotations", json={"body": "   \t  "}
        ).status_code
        == 422
    )


def test_create_annotation_oversize_body_is_422(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))
    resp = client.post(
        f"/api/library/documents/{doc.id}/annotations",
        json={"body": "x" * 4001},
    )
    assert resp.status_code == 422


def test_create_annotation_strips_control_chars_keeps_newlines(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))
    resp = client.post(
        f"/api/library/documents/{doc.id}/annotations",
        json={"body": "line one\nline\x00 two", "anchor": "a\x07b"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["body"] == "line one\nline two"  # NUL removed, newline kept
    assert body["anchor"] == "ab"  # control char stripped from single-line anchor


def test_update_annotation_edits_in_place(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))
    created = client.post(
        f"/api/library/documents/{doc.id}/annotations",
        json={"body": "original", "anchor": "x"},
    ).json()

    resp = client.patch(
        f"/api/library/documents/{doc.id}/annotations/{created['id']}",
        json={"body": "edited"},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["body"] == "edited"
    assert updated["anchor"] == "x"  # unchanged when omitted
    assert updated["id"] == created["id"]


def test_update_missing_annotation_is_404(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))
    resp = client.patch(
        f"/api/library/documents/{doc.id}/annotations/nope",
        json={"body": "edited"},
    )
    assert resp.status_code == 404


def test_delete_annotation_then_idempotent(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))
    created = client.post(
        f"/api/library/documents/{doc.id}/annotations", json={"body": "note"}
    ).json()

    assert (
        client.delete(
            f"/api/library/documents/{doc.id}/annotations/{created['id']}"
        ).status_code
        == 204
    )
    assert client.get(f"/api/library/documents/{doc.id}/annotations").json() == []
    # Deleting an already-gone annotation still succeeds.
    assert (
        client.delete(
            f"/api/library/documents/{doc.id}/annotations/{created['id']}"
        ).status_code
        == 204
    )


def test_annotations_are_owner_only(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))
    client.post(
        f"/api/library/documents/{doc.id}/annotations", json={"body": "private note"}
    )
    other = {"X-Dev-User": "mallory"}

    # Another user can neither read, add, edit, nor delete — all generic 404.
    assert (
        client.get(
            f"/api/library/documents/{doc.id}/annotations", headers=other
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/library/documents/{doc.id}/annotations",
            json={"body": "intrusion"},
            headers=other,
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/library/documents/{doc.id}/annotations/whatever", headers=other
        ).status_code
        == 404
    )
    # The owner's note is untouched by the refused access.
    assert len(client.get(f"/api/library/documents/{doc.id}/annotations").json()) == 1


def test_annotations_excluded_from_document_summary(client):
    # Notes are owner-private and must not bleed into the model-facing summary.
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))
    client.post(
        f"/api/library/documents/{doc.id}/annotations", json={"body": "secret note"}
    )
    summary = client.get("/api/library/documents").json()
    assert summary and "annotations" not in summary[0]
