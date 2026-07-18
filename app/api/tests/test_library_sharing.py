"""Document-level sharing spine: owner-only grant/revoke endpoints,
the "shared with me" listing, cross-owner read access for grantees, and the
privacy guarantees (non-owners can't see/alter shares; private docs never leak;
owner-private artifacts don't travel).

Grants are keyed on *email* (the universal principal); ownership/partitioning
stays on ``internal_user_id``. The dev auth provider maps ``X-Dev-User: bob`` to
email ``bob@example.com``, so two headers simulate owner + grantee.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.auth.base import AuthCredentials
from ai4ia_api.library.models import UserDocument, Visibility
from ai4ia_api.library.repository import DocumentConflictError, DocumentNotFoundError
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


def _seed(client: TestClient, **kw) -> UserDocument:
    doc = UserDocument(**kw)
    asyncio.run(client.app.state.document_library.create_document(doc))
    return doc


# --- grant / read-access / list / revoke happy path ---
def test_share_grant_grantee_reads_then_revoke(client):
    owner = _uid(client)  # default dev user (dev@example.com)
    doc = _seed(client, userId=owner, filename="plan.pdf", size=10)
    bob = {"X-Dev-User": "bob"}

    # Before sharing, bob can't see it.
    assert client.get(f"/api/library/documents/{doc.id}", headers=bob).status_code == 404
    assert client.get("/api/library/shared", headers=bob).json() == []

    # Owner grants bob.
    put = client.put(
        f"/api/library/documents/{doc.id}/shares",
        json={"visibility": "shared", "grantees": ["bob@example.com"]},
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["visibility"] == "shared"
    assert body["grantees"] == ["bob@example.com"]

    # Bob can now open it (cross-owner access) and it appears in his "shared with me".
    assert client.get(f"/api/library/documents/{doc.id}", headers=bob).status_code == 200
    shared = client.get("/api/library/shared", headers=bob).json()
    assert [d["id"] for d in shared] == [doc.id]

    # Owner revokes; bob loses access immediately.
    rev = client.delete(f"/api/library/documents/{doc.id}/shares/bob@example.com")
    assert rev.status_code == 200 and rev.json()["grantees"] == []
    assert client.get(f"/api/library/documents/{doc.id}", headers=bob).status_code == 404
    assert client.get("/api/library/shared", headers=bob).json() == []


def test_shared_document_selection_conversion_and_revocation(client):
    owner = _uid(client)
    bob_headers = {"X-Dev-User": "bob"}
    bob_id = _uid(client, "bob")
    shared = _seed(client, userId=owner, filename="shared.pdf", size=10)
    owned = _seed(client, userId=bob_id, filename="owned.pdf", size=10)
    client.put(
        f"/api/library/documents/{shared.id}/shares",
        json={"visibility": "shared", "grantees": ["bob@example.com"]},
    )

    explicit = client.post(
        "/api/sessions",
        headers=bob_headers,
        json={"model": "gpt-5.2", "libraryDocumentIds": [shared.id]},
    )
    assert explicit.status_code == 201, explicit.text
    session_id = explicit.json()["id"]
    inspector = client.get(
        f"/api/sessions/{session_id}/inspector", headers=bob_headers
    ).json()
    assert [document["id"] for document in inspector["libraryDocuments"]] == [
        shared.id
    ]

    legacy = client.post(
        "/api/sessions", headers=bob_headers, json={"model": "gpt-5.2"}
    ).json()
    converted = client.delete(
        f"/api/sessions/{legacy['id']}/library-documents/{owned.id}",
        headers=bob_headers,
    )
    assert converted.status_code == 200, converted.text
    assert converted.json()["libraryDocumentIds"] == [shared.id]

    client.delete(f"/api/library/documents/{shared.id}/shares/bob@example.com")
    stale = client.get(
        f"/api/sessions/{session_id}/inspector", headers=bob_headers
    ).json()
    assert stale["libraryDocuments"] == []
    rejected = client.patch(
        f"/api/sessions/{session_id}",
        headers=bob_headers,
        json={"libraryDocumentIds": [shared.id]},
    )
    assert rejected.status_code == 422
    assert (
        client.post(
            f"/api/sessions/{session_id}/library-documents/{shared.id}",
            headers=bob_headers,
        ).status_code
        == 404
    )

# --- owner-only control surface ---
def test_shares_endpoints_are_owner_only(client):
    owner = _uid(client)
    doc = _seed(client, userId=owner, filename="o.pdf")
    bob = {"X-Dev-User": "bob"}

    # Even with read access granted, a grantee can't see or change the shares.
    client.put(
        f"/api/library/documents/{doc.id}/shares",
        json={"visibility": "shared", "grantees": ["bob@example.com"]},
    )
    assert client.get(f"/api/library/documents/{doc.id}/shares", headers=bob).status_code == 404
    assert (
        client.put(
            f"/api/library/documents/{doc.id}/shares",
            json={"visibility": "shared", "grantees": ["mallory@example.com"]},
            headers=bob,
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/library/documents/{doc.id}/shares/bob@example.com", headers=bob
        ).status_code
        == 404
    )
    # Owner's grant is untouched by the rejected non-owner calls.
    assert client.get(f"/api/library/documents/{doc.id}/shares").json()["grantees"] == [
        "bob@example.com"
    ]


# --- normalization: self-share dropped, dupes/blanks collapsed, case-folded ---
def test_grant_normalizes_grantees(client):
    owner = _uid(client)
    doc = _seed(client, userId=owner, filename="n.pdf")
    put = client.put(
        f"/api/library/documents/{doc.id}/shares",
        json={
            "visibility": "shared",
            "grantees": [
                "dev@example.com",  # owner-self -> dropped
                "  BOB@Example.com ",  # normalized
                "bob@example.com",  # dup -> collapsed
                "",  # blank -> dropped
                "carol@example.com",
            ],
        },
    )
    assert put.status_code == 200, put.text
    assert put.json()["grantees"] == ["bob@example.com", "carol@example.com"]


# --- validation: malformed email + over-cap both 422 ---
def test_grant_rejects_bad_email_and_overcap(client):
    owner = _uid(client)
    doc = _seed(client, userId=owner, filename="v.pdf")
    bad = client.put(
        f"/api/library/documents/{doc.id}/shares",
        json={"visibility": "shared", "grantees": ["not-an-email"]},
    )
    assert bad.status_code == 422

    too_many = [f"user{i}@example.com" for i in range(101)]
    over = client.put(
        f"/api/library/documents/{doc.id}/shares",
        json={"visibility": "shared", "grantees": too_many},
    )
    assert over.status_code == 422


# --- flipping away from shared clears the ACL ---
def test_visibility_change_clears_acl(client):
    owner = _uid(client)
    doc = _seed(client, userId=owner, filename="c.pdf")
    client.put(
        f"/api/library/documents/{doc.id}/shares",
        json={"visibility": "shared", "grantees": ["bob@example.com"]},
    )
    private = client.put(
        f"/api/library/documents/{doc.id}/shares",
        json={"visibility": "private", "grantees": ["bob@example.com"]},
    )
    assert private.status_code == 200
    assert private.json() == {
        "documentId": doc.id,
        "visibility": "private",
        "grantees": [],
    }


# --- private docs never leak; tenant-public is openable but not auto-listed ---
def test_private_not_leaked_public_openable_but_unlisted(client):
    owner = _uid(client)
    private = _seed(client, userId=owner, filename="secret.pdf", visibility=Visibility.private)
    public = _seed(client, userId=owner, filename="memo.pdf", visibility=Visibility.public)
    bob = {"X-Dev-User": "bob"}

    assert client.get(f"/api/library/documents/{private.id}", headers=bob).status_code == 404
    # Tenant-public: any authenticated user can open it by id.
    assert client.get(f"/api/library/documents/{public.id}", headers=bob).status_code == 200
    # ...but it is deliberately NOT auto-listed in "shared with me".
    assert client.get("/api/library/shared", headers=bob).json() == []


# --- revoke is idempotent; "shared with me" excludes the caller's own docs ---
def test_revoke_idempotent_and_shared_excludes_self(client):
    owner = _uid(client)
    doc = _seed(client, userId=owner, filename="i.pdf", visibility=Visibility.shared,
                acl=["bob@example.com"])
    # Revoking a non-grantee is a harmless no-op returning current state.
    resp = client.delete(f"/api/library/documents/{doc.id}/shares/nobody@example.com")
    assert resp.status_code == 200 and resp.json()["grantees"] == ["bob@example.com"]

    # The owner never sees their own shared doc in "shared with me".
    assert client.get("/api/library/shared").json() == []


# --- concurrent-delete race: update_document can still raise DocumentNotFoundError
# after the router's own ownership check passed (the document vanished in the
# gap). Both share-mutating endpoints must translate that into a 404, matching
# every other document-not-found path, instead of letting it bubble up as a
# raw 500 from the app's generic exception handler. ---
class _UpdateRacesDeleteRepo:
    """Wraps a real repo; ``update_document`` raises as if a concurrent delete
    landed between the router's ownership read and this write."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def update_document(self, document):
        raise DocumentNotFoundError(document.id)


def test_set_shares_document_deleted_concurrently_is_404(client):
    owner = _uid(client)
    doc = _seed(client, userId=owner, filename="race.pdf")
    client.app.state.document_library = _UpdateRacesDeleteRepo(
        client.app.state.document_library
    )
    resp = client.put(
        f"/api/library/documents/{doc.id}/shares",
        json={"visibility": "shared", "grantees": ["bob@example.com"]},
    )
    assert resp.status_code == 404


def test_revoke_share_document_deleted_concurrently_is_404(client):
    owner = _uid(client)
    doc = _seed(
        client,
        userId=owner,
        filename="race.pdf",
        visibility=Visibility.shared,
        acl=["bob@example.com"],
    )
    client.app.state.document_library = _UpdateRacesDeleteRepo(
        client.app.state.document_library
    )
    resp = client.delete(f"/api/library/documents/{doc.id}/shares/bob@example.com")
    assert resp.status_code == 404


# --- etag conflict: update_document can also lose an optimistic-concurrency
# race (the document still exists but was modified since the router's own
# read). That must surface as 409, distinct from the 404 case above — a
# stale-write conflict is not a not-found, and reporting it as one would be a
# false negative that could mislead a client into thinking the document is
# gone. ---
class _UpdateRacesConflictRepo:
    """Wraps a real repo; ``update_document`` raises as if the document was
    modified (etag moved) between the router's ownership read and this write."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def update_document(self, document):
        raise DocumentConflictError(document.id)


def test_set_shares_document_modified_concurrently_is_409(client):
    owner = _uid(client)
    doc = _seed(client, userId=owner, filename="race.pdf")
    client.app.state.document_library = _UpdateRacesConflictRepo(
        client.app.state.document_library
    )
    resp = client.put(
        f"/api/library/documents/{doc.id}/shares",
        json={"visibility": "shared", "grantees": ["bob@example.com"]},
    )
    assert resp.status_code == 409


def test_revoke_share_document_modified_concurrently_is_409(client):
    owner = _uid(client)
    doc = _seed(
        client,
        userId=owner,
        filename="race.pdf",
        visibility=Visibility.shared,
        acl=["bob@example.com"],
    )
    client.app.state.document_library = _UpdateRacesConflictRepo(
        client.app.state.document_library
    )
    resp = client.delete(f"/api/library/documents/{doc.id}/shares/bob@example.com")
    assert resp.status_code == 409
