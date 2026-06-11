"""POST /api/library/documents/{id}/memory (Phase 11E-1): the explicit
save-to-memory action.

A real :class:`MemoryService` (fake embedder + in-memory store) is attached to
``app.state.memory`` so we can assert the 201 happy path stores ``kind="document"``
records, while exercising the flag/owner/status/memory gates. All IO is in-memory;
no network."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.auth.base import AuthCredentials
from ai4ia_api.library.blob_store import PARSED_NAME, blob_path
from ai4ia_api.library.models import DocumentStatus, UserDocument
from ai4ia_api.main import create_app
from ai4ia_api.memory.in_memory import InMemoryVectorStore
from ai4ia_api.memory.service import MemoryService
from tests.conftest import make_settings


class FakeEmbedder:
    """Every text maps to the same unit vector, so cosine similarity is 1.0 and
    recall returns whatever was stored (above the default min_score)."""

    async def embed(self, inputs):
        return [[1.0, 0.0, 0.0] for _ in inputs]

    async def embed_one(self, text):
        return [1.0, 0.0, 0.0]


def _client(*, memory: bool = True, **overrides) -> TestClient:
    app = create_app(make_settings(document_understanding_enabled=True, **overrides))
    c = TestClient(app)
    c.__enter__()
    if memory:
        c.app.state.memory = MemoryService(
            store=InMemoryVectorStore(), embedder=FakeEmbedder()
        )
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
    summary: str = "Quarterly revenue report",
    parsed: str | None = "# Report\n\nRevenue grew twenty percent this quarter.",
) -> UserDocument:
    doc = UserDocument(userId=uid, filename="report.pdf", status=status, summary=summary)
    if parsed is not None:
        blob = client.app.state.document_ingestor.blob
        path = blob_path(uid, doc.id, PARSED_NAME)
        await blob.put(path, parsed.encode("utf-8"), "text/markdown")
        doc.parsedPath = path
    await client.app.state.document_library.create_document(doc)
    return doc


def test_save_to_memory_happy_path_stores_document_records(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))

    resp = client.post(f"/api/library/documents/{doc.id}/memory")
    assert resp.status_code == 201, resp.text
    assert resp.json()["saved"] >= 1

    # The records landed in the user's memory, attributed kind="document".
    hits = asyncio.run(client.app.state.memory.recall(uid, "anything"))
    assert hits and all(h.kind == "document" for h in hits)
    assert "Quarterly revenue report" in {h.text for h in hits}


def test_save_to_memory_summary_only_when_no_parsed(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid, parsed=None))
    resp = client.post(f"/api/library/documents/{doc.id}/memory")
    assert resp.status_code == 201, resp.text
    assert resp.json()["saved"] == 1  # only the summary card


def test_save_to_memory_404_when_library_disabled():
    c = TestClient(create_app(make_settings()))  # document understanding OFF
    c.__enter__()
    try:
        assert c.post("/api/library/documents/whatever/memory").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_save_to_memory_409_when_memory_disabled():
    c = _client(memory=False)  # app.state.memory stays the Noop service (disabled)
    try:
        uid = _uid(c)
        doc = asyncio.run(_seed(c, uid=uid))
        assert c.post(f"/api/library/documents/{doc.id}/memory").status_code == 409
    finally:
        c.__exit__(None, None, None)


def test_save_to_memory_409_when_not_ready(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid, status=DocumentStatus.stored))
    assert client.post(f"/api/library/documents/{doc.id}/memory").status_code == 409


def test_save_to_memory_404_for_other_users_document(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))
    other = {"X-Dev-User": "mallory"}
    resp = client.post(f"/api/library/documents/{doc.id}/memory", headers=other)
    assert resp.status_code == 404
    # Nothing was written into the attacker's (or owner's) memory by the refusal.
    assert asyncio.run(client.app.state.memory.recall(uid, "anything")) == []


def test_save_to_memory_422_when_document_has_no_content(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid, summary="", parsed=None))
    assert client.post(f"/api/library/documents/{doc.id}/memory").status_code == 422


# --- 11E-3: idempotent re-save + forget-by-document ---


def test_resaving_same_document_is_idempotent(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))

    first = client.post(f"/api/library/documents/{doc.id}/memory")
    assert first.status_code == 201, first.text
    saved = first.json()["saved"]

    second = client.post(f"/api/library/documents/{doc.id}/memory")
    assert second.status_code == 201, second.text
    assert second.json()["saved"] == saved

    # Re-save replaced rather than duplicated: the recall set is the same size as
    # a single save, not double.
    hits = asyncio.run(client.app.state.memory.recall(uid, "anything"))
    assert len(hits) == saved


def test_forget_document_from_memory_removes_only_that_document(client):
    uid = _uid(client)
    keep = asyncio.run(_seed(client, uid=uid, summary="Keep me", parsed=None))
    drop = asyncio.run(_seed(client, uid=uid, summary="Forget me", parsed=None))
    client.post(f"/api/library/documents/{keep.id}/memory")
    client.post(f"/api/library/documents/{drop.id}/memory")

    resp = client.delete(f"/api/library/documents/{drop.id}/memory")
    assert resp.status_code == 200, resp.text
    assert resp.json()["forgotten"] == 1

    texts = {h.text for h in asyncio.run(client.app.state.memory.recall(uid, "anything"))}
    assert texts == {"Keep me"}

    # Idempotent: forgetting again removes nothing.
    again = client.delete(f"/api/library/documents/{drop.id}/memory")
    assert again.status_code == 200
    assert again.json()["forgotten"] == 0


def test_forget_document_404_when_library_disabled():
    c = TestClient(create_app(make_settings()))  # document understanding OFF
    c.__enter__()
    try:
        assert c.delete("/api/library/documents/whatever/memory").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_forget_document_409_when_memory_disabled():
    c = _client(memory=False)  # Noop memory service (disabled)
    try:
        uid = _uid(c)
        doc = asyncio.run(_seed(c, uid=uid))
        assert c.delete(f"/api/library/documents/{doc.id}/memory").status_code == 409
    finally:
        c.__exit__(None, None, None)


def test_forget_document_404_for_other_users_document(client):
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))
    client.post(f"/api/library/documents/{doc.id}/memory")
    other = {"X-Dev-User": "mallory"}
    resp = client.delete(f"/api/library/documents/{doc.id}/memory", headers=other)
    assert resp.status_code == 404
    # The owner's saved memory is untouched by the refused forget.
    assert asyncio.run(client.app.state.memory.recall(uid, "anything"))


def test_forget_document_allowed_regardless_of_status(client):
    # Unlike save, forget has no ready-status gate: a document that has since
    # moved out of "ready" can still have its memories forgotten.
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid, summary="Stored gist", parsed=None))
    client.post(f"/api/library/documents/{doc.id}/memory")
    # Flip the document out of ready; forgetting must still work.
    doc.status = DocumentStatus.stored
    asyncio.run(client.app.state.document_library.update_document(doc))
    resp = client.delete(f"/api/library/documents/{doc.id}/memory")
    assert resp.status_code == 200, resp.text
    assert resp.json()["forgotten"] == 1


# --- delete cascades to memory (erase hardening) ---


def test_delete_document_cascades_forget_from_memory(client):
    # Deleting a document is a complete erase: it also forgets anything that
    # document contributed to durable memory, mirroring the blob/chunk purge.
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid, summary="Forget on delete", parsed=None))
    client.post(f"/api/library/documents/{doc.id}/memory")
    assert asyncio.run(client.app.state.memory.recall(uid, "anything"))  # precondition

    resp = client.delete(f"/api/library/documents/{doc.id}")
    assert resp.status_code == 204, resp.text
    assert asyncio.run(client.app.state.memory.recall(uid, "anything")) == []


def test_delete_document_forgets_only_that_documents_memory(client):
    uid = _uid(client)
    keep = asyncio.run(_seed(client, uid=uid, summary="Keep me", parsed=None))
    drop = asyncio.run(_seed(client, uid=uid, summary="Drop me", parsed=None))
    client.post(f"/api/library/documents/{keep.id}/memory")
    client.post(f"/api/library/documents/{drop.id}/memory")

    assert client.delete(f"/api/library/documents/{drop.id}").status_code == 204

    texts = {h.text for h in asyncio.run(client.app.state.memory.recall(uid, "anything"))}
    assert texts == {"Keep me"}


def test_delete_document_succeeds_when_memory_disabled():
    c = _client(memory=False)  # Noop memory service (disabled) -> cascade is a no-op
    try:
        uid = _uid(c)
        doc = asyncio.run(_seed(c, uid=uid, parsed=None))
        assert c.delete(f"/api/library/documents/{doc.id}").status_code == 204
    finally:
        c.__exit__(None, None, None)


def test_delete_document_survives_memory_forget_failure(client):
    # The manifest delete is the source of truth; a transient memory failure in
    # the best-effort cascade must never block the delete.
    from ai4ia_api.library.repository import DocumentNotFoundError

    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid, parsed=None))
    client.post(f"/api/library/documents/{doc.id}/memory")

    async def boom(*args, **kwargs):
        raise RuntimeError("memory down")

    client.app.state.memory.forget_document = boom  # type: ignore[assignment]
    assert client.delete(f"/api/library/documents/{doc.id}").status_code == 204
    with pytest.raises(DocumentNotFoundError):
        asyncio.run(client.app.state.document_library.get_document(uid, doc.id))
