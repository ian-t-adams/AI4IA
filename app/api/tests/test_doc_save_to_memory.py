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
