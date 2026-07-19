"""Per-user document library API: default-OFF refusal, the analyzer
registry CRUD, ownership isolation across users, and document read/delete.

The library has no upload endpoint in 11A (CU ingest lands in 11B), so documents
are seeded directly into the in-memory repo using the dev user's resolved
``internal_user_id`` to exercise the read/delete + ownership paths over HTTP.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.auth.base import AuthCredentials
from ai4ia_api.library.models import BUILTIN_ANALYZER_IDS, UserDocument
from ai4ia_api.main import create_app
from tests.conftest import make_settings


def _client(**overrides) -> TestClient:
    app = create_app(make_settings(**overrides))
    c = TestClient(app)
    c.__enter__()
    return c


@pytest.fixture
def client():
    c = _client(document_understanding_enabled=True)
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _uid(client: TestClient, sub: str | None = None) -> str:
    headers = {"X-Dev-User": sub} if sub else {}
    provider = client.app.state.auth_provider
    user = asyncio.run(provider.authenticate(AuthCredentials(headers=headers)))
    return user.internal_user_id


# --- default-OFF posture ---
def test_library_disabled_by_default_returns_404():
    c = _client()  # document_understanding_enabled defaults to False
    try:
        assert c.get("/api/library/documents").status_code == 404
        assert c.get("/api/library/analyzers").status_code == 404
        assert c.post("/api/library/analyzers", json={"name": "x"}).status_code == 404
    finally:
        c.__exit__(None, None, None)


# --- documents ---
def test_documents_list_empty_then_seeded(client):
    assert client.get("/api/library/documents").json() == []

    uid = _uid(client)
    seeded = UserDocument(userId=uid, filename="report.pdf", size=10)
    asyncio.run(client.app.state.document_library.create_document(seeded))

    listed = client.get("/api/library/documents")
    assert listed.status_code == 200
    body = listed.json()
    assert [d["id"] for d in body] == [seeded.id]
    assert "acl" not in body[0]  # reserved field not surfaced

    got = client.get(f"/api/library/documents/{seeded.id}")
    assert got.status_code == 200 and got.json()["filename"] == "report.pdf"


def test_get_unknown_document_404(client):
    assert client.get("/api/library/documents/missing").status_code == 404


def test_document_ownership_isolation_over_http(client):
    uid = _uid(client)
    seeded = UserDocument(userId=uid, filename="secret.pdf")
    asyncio.run(client.app.state.document_library.create_document(seeded))

    other = {"X-Dev-User": "mallory"}
    assert client.get(f"/api/library/documents/{seeded.id}", headers=other).status_code == 404
    assert client.get("/api/library/documents", headers=other).json() == []
    # A non-owner delete is idempotent (the doc is invisible to her, so "already
    # gone" -> 204) and must NOT remove the owner's document.
    assert client.delete(f"/api/library/documents/{seeded.id}", headers=other).status_code == 204
    assert client.get(f"/api/library/documents/{seeded.id}").status_code == 200


def test_delete_document(client):
    uid = _uid(client)
    seeded = UserDocument(userId=uid, filename="d.pdf")
    asyncio.run(client.app.state.document_library.create_document(seeded))
    assert client.delete(f"/api/library/documents/{seeded.id}").status_code == 204
    assert client.get(f"/api/library/documents/{seeded.id}").status_code == 404
    # Idempotent.
    assert client.delete(f"/api/library/documents/{seeded.id}").status_code == 204


# --- analyzers ---
def test_analyzers_list_returns_builtins(client):
    resp = client.get("/api/library/analyzers")
    assert resp.status_code == 200
    ids = {a["id"] for a in resp.json()}
    assert BUILTIN_ANALYZER_IDS <= ids


def test_create_get_delete_custom_analyzer(client):
    created = client.post(
        "/api/library/analyzers",
        json={"name": "Invoices", "description": "vendor invoices", "modalities": ["document"]},
    )
    assert created.status_code == 201, created.text
    aid = created.json()["id"]
    assert created.json()["kind"] == "custom"

    assert client.get(f"/api/library/analyzers/{aid}").status_code == 200
    assert aid in {a["id"] for a in client.get("/api/library/analyzers").json()}

    assert client.delete(f"/api/library/analyzers/{aid}").status_code == 204
    assert client.get(f"/api/library/analyzers/{aid}").status_code == 404


def test_custom_analyzer_isolated_by_user(client):
    created = client.post("/api/library/analyzers", json={"name": "Mine"})
    aid = created.json()["id"]
    other = {"X-Dev-User": "stranger"}
    assert client.get(f"/api/library/analyzers/{aid}", headers=other).status_code == 404
    other_ids = {a["id"] for a in client.get("/api/library/analyzers", headers=other).json()}
    assert aid not in other_ids
    assert BUILTIN_ANALYZER_IDS <= other_ids


def test_builtin_analyzer_not_deletable_over_http(client):
    builtin_id = next(iter(BUILTIN_ANALYZER_IDS))
    assert client.delete(f"/api/library/analyzers/{builtin_id}").status_code == 404
    assert client.get(f"/api/library/analyzers/{builtin_id}").status_code == 200


def test_create_analyzer_accepts_valid_base_analyzer_id(client):
    resp = client.post(
        "/api/library/analyzers",
        json={"name": "Invoices", "baseAnalyzerId": "prebuilt-documentSearch"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["baseAnalyzerId"] == "prebuilt-documentSearch"


@pytest.mark.parametrize(
    "bad_id",
    [
        "../secrets",
        "foo/bar",
        "foo?x=1",
        "foo#frag",
        "foo bar",
        "a" * 129,
        "",
    ],
)
def test_create_analyzer_rejects_unsafe_base_analyzer_id(client, bad_id):
    # baseAnalyzerId is interpolated directly into the Content Understanding
    # request URL, so path/query-breaking characters must be rejected at the
    # API boundary (422) rather than reaching the CU client.
    resp = client.post(
        "/api/library/analyzers",
        json={"name": "Invoices", "baseAnalyzerId": bad_id},
    )
    assert resp.status_code == 422


def test_create_analyzer_accepts_base_analyzer_id_with_dot_and_at_64_chars(client):
    # The Content Understanding analyzer-id contract allows dots and up to 64
    # characters (ai4ia_api.content_understanding.models.ANALYZER_ID_RE).
    value = "a" * 63 + "."
    resp = client.post(
        "/api/library/analyzers",
        json={"name": "Invoices", "baseAnalyzerId": value},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["baseAnalyzerId"] == value


def test_create_analyzer_rejects_base_analyzer_id_over_64_chars(client):
    resp = client.post(
        "/api/library/analyzers",
        json={"name": "Invoices", "baseAnalyzerId": "a" * 65},
    )
    assert resp.status_code == 422


def test_create_analyzer_rejects_base_analyzer_id_with_trailing_newline(client):
    # Regression: a validator built on ``match(pattern + "$")`` instead of
    # ``fullmatch`` would incorrectly accept this, because "$" alone matches
    # just before a trailing newline.
    resp = client.post(
        "/api/library/analyzers",
        json={"name": "Invoices", "baseAnalyzerId": "prebuilt-invoice\n"},
    )
    assert resp.status_code == 422


def test_create_analyzer_accepts_base_analyzer_id_not_starting_with_alnum(client):
    # Unlike the previous validator (which required an alphanumeric first
    # character), the CU analyzer-id contract
    # (fullmatch(r'[A-Za-z0-9._-]{1,64}', value)) allows '.', '_' and '-' in
    # any position, including first.
    value = "-custom.analyzer_v2"
    resp = client.post(
        "/api/library/analyzers",
        json={"name": "Invoices", "baseAnalyzerId": value},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["baseAnalyzerId"] == value
