"""POST /api/library/documents/{id}/memory: the explicit save-to-memory action.

A real :class:`MemoryService` (fake embedder + in-memory store) is attached to
``app.state.memory`` so we can assert the 201 happy path stores ``kind="document"``
records, while exercising the flag/owner/status/memory gates. All IO is in-memory;
no network."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
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


async def _auth_user(client: TestClient, sub: str | None = None):
    headers = {"X-Dev-User": sub} if sub else {}
    provider = client.app.state.auth_provider
    return await provider.authenticate(AuthCredentials(headers=headers))


async def _gather_race(*coros):
    """asyncio.gather must be awaited from inside a running loop; this
    lets callers drive it with a single asyncio.run(...)."""
    return await asyncio.gather(*coros)


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


def test_delete_document_aborts_when_memory_forget_fails():
    """HIGH finding (round 5): the manifest delete used to proceed even when
    the memory-forget cascade failed, silently leaving the document's saved
    memories permanently recallable with no manifest left to retry the erase
    against. The fix must erase memory FIRST and abort (502, manifest intact)
    on failure, so the delete stays retryable instead of losing the handle."""
    c = _client()
    try:
        uid = _uid(c)
        doc = asyncio.run(_seed(c, uid=uid, parsed=None))
        c.post(f"/api/library/documents/{doc.id}/memory")

        async def boom(*args, **kwargs):
            raise RuntimeError("memory down")

        c.app.state.memory.forget_document = boom  # type: ignore[assignment]
        resp = c.delete(f"/api/library/documents/{doc.id}")
        assert resp.status_code == 502, resp.text

        # The manifest survives - the delete is retryable once memory recovers.
        refreshed = asyncio.run(c.app.state.document_library.get_document(uid, doc.id))
        assert refreshed.id == doc.id
    finally:
        c.__exit__(None, None, None)


def test_delete_document_succeeds_after_memory_forget_recovers():
    """Companion to the abort test: once the transient memory failure clears,
    retrying the same delete call completes normally (manifest deleted, memory
    forgotten) - proving the abort above genuinely preserved retryability
    rather than leaving the document stuck."""
    c = _client()
    try:
        uid = _uid(c)
        doc = asyncio.run(_seed(c, uid=uid, parsed=None))
        c.post(f"/api/library/documents/{doc.id}/memory")

        real_forget = c.app.state.memory.forget_document
        calls = {"n": 0}

        async def flaky_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("memory down")
            return await real_forget(*args, **kwargs)

        c.app.state.memory.forget_document = flaky_once  # type: ignore[assignment]

        first = c.delete(f"/api/library/documents/{doc.id}")
        assert first.status_code == 502, first.text

        second = c.delete(f"/api/library/documents/{doc.id}")
        assert second.status_code == 204, second.text
        assert asyncio.run(c.app.state.memory.recall(uid, "anything")) == []
        from ai4ia_api.library.repository import DocumentNotFoundError

        with pytest.raises(DocumentNotFoundError):
            asyncio.run(c.app.state.document_library.get_document(uid, doc.id))
    finally:
        c.__exit__(None, None, None)


# --- production defect: pgvector's erase_document cannot key on document_id ---


class _NoDocumentEraseStore(InMemoryVectorStore):
    """Stands in for PgVectorStore: every op works except erase_document,
    which raises NotImplementedError instead of the store's old false-success
    no-op (see memory/pgvector_store.py)."""

    async def erase_document(self, user_id: str, document_id: str) -> int:
        raise NotImplementedError("document-scoped erase is not supported")


def test_save_to_memory_502_when_store_cannot_erase_by_document(client):
    """Production defect: PgVectorStore.erase_document used to report a false
    success (0 removed) while a document's memories stayed fully recallable,
    so remember_document's idempotent re-save silently duplicated them on
    every repeat save. It now raises instead of lying; the explicit save
    endpoint must surface that as 502 ("memory failures surface because the
    user explicitly asked to save"), never a false 201."""
    client.app.state.memory = MemoryService(
        store=_NoDocumentEraseStore(), embedder=FakeEmbedder()
    )
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))
    resp = client.post(f"/api/library/documents/{doc.id}/memory")
    assert resp.status_code == 502, resp.text


def test_forget_document_502_when_store_cannot_erase_by_document(client):
    """Same production defect from the forget side: forgetting a document's
    memories must surface an honest 502, not a false ``forgotten: 0``, when
    the backing store cannot key an erase on document_id."""
    client.app.state.memory = MemoryService(
        store=_NoDocumentEraseStore(), embedder=FakeEmbedder()
    )
    uid = _uid(client)
    doc = asyncio.run(_seed(client, uid=uid))
    resp = client.delete(f"/api/library/documents/{doc.id}/memory")
    assert resp.status_code == 502, resp.text


def test_delete_document_502_when_store_cannot_erase_by_document():
    """Same production defect, reached through the delete cascade: a backend
    that cannot key an erase on document_id (pgvector today) must abort the
    whole delete with 502 rather than removing the manifest while the
    document's memories stay permanently recallable with no handle left to
    retry against."""
    c = _client()
    try:
        c.app.state.memory = MemoryService(
            store=_NoDocumentEraseStore(), embedder=FakeEmbedder()
        )
        uid = _uid(c)
        doc = asyncio.run(_seed(c, uid=uid, parsed=None))
        resp = c.delete(f"/api/library/documents/{doc.id}")
        assert resp.status_code == 502, resp.text
        refreshed = asyncio.run(c.app.state.document_library.get_document(uid, doc.id))
        assert refreshed.id == doc.id
    finally:
        c.__exit__(None, None, None)


# --- round 6 HIGH acceptance finding: save-vs-delete tombstone fence ---


def test_save_to_memory_self_compensates_when_concurrent_delete_wins_race():
    """Round 6 HIGH: save_document_to_memory's initial get_document can
    succeed, then -- while its own memory write is still in flight -- a
    concurrent delete_document call can tombstone, forget, and hard-delete
    the very same document to completion before the save's post-write
    recheck ever runs. Without a fence the save would report a false 201 for
    memories that are, by the time the response is sent, already permanently
    unreachable via any future forget-by-document_id call (the manifest --
    and thus the only retryable handle for a document-scoped erase -- is
    already gone).

    Drives the router handlers directly (bypassing HTTP/DI, matching
    test_session_concurrency.py's add_message-vs-delete_session tests) so
    the interleaving is deterministic: an explicit gate forces
    delete_document to run to full completion strictly between save's own
    memory write landing and its post-write recheck.
    """
    from ai4ia_api.library.repository import DocumentNotFoundError
    from ai4ia_api.routers import library as library_router

    c = _client()
    try:
        uid = _uid(c)
        doc = asyncio.run(_seed(c, uid=uid, parsed=None))
        user = asyncio.run(_auth_user(c))
        fake_request = SimpleNamespace(app=c.app)

        memory = c.app.state.memory
        real_remember = memory.remember_document
        delete_done = asyncio.Event()

        async def remember_then_wait_for_delete(*args, **kwargs):
            saved = await real_remember(*args, **kwargs)
            # Force the concurrent delete_document below to run to full
            # completion here, strictly between this save's memory write
            # landing and its own post-write recheck.
            await asyncio.wait_for(delete_done.wait(), timeout=5)
            return saved

        memory.remember_document = remember_then_wait_for_delete  # type: ignore[assignment]

        async def run_delete():
            await library_router.delete_document(
                document_id=doc.id, request=fake_request, user=user
            )
            delete_done.set()

        async def run_save():
            try:
                return await library_router.save_document_to_memory(
                    document_id=doc.id, request=fake_request, user=user
                )
            except HTTPException as exc:
                return exc

        save_result, _ = asyncio.run(_gather_race(run_save(), run_delete()))

        assert isinstance(save_result, HTTPException), save_result
        assert save_result.status_code == 404

        # Self-compensation must have actually forgotten what this save just
        # wrote -- nothing recallable -- and the manifest stays gone (the
        # delete, not the save, owns removing it).
        assert asyncio.run(memory.recall(uid, "anything")) == []
        with pytest.raises(DocumentNotFoundError):
            asyncio.run(c.app.state.document_library.get_document(uid, doc.id))
    finally:
        c.__exit__(None, None, None)
