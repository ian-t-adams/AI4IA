"""Cosmos repo parity (Phase 11A/11B): ``update_document`` must raise on a
missing or cross-user id rather than silently create/resurrect it — matching the
in-memory repo contract that the 11B ingest worker relies on when driving status
transitions (stored -> ready/failed). The write is an ETag-conditional
``replace_item`` so a delete that lands between the read and the write also loses
(no resurrection), closing the delete-during-enrich TOCTOU.

The Cosmos client + AAD credential are constructed in ``__init__``; we bypass it
with ``object.__new__`` and inject a minimal async fake container so the parity
logic is exercised without any live network or managed identity.
"""
from __future__ import annotations

import pytest
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceNotFoundError,
)

from ai4ia_api.library.cosmos_repo import CosmosDocumentLibraryRepository
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.library.models import UserDocument
from ai4ia_api.library.repository import DocumentNotFoundError


class _FakeDocs:
    """Minimal async stand-in for a Cosmos container client."""

    def __init__(self, existing: dict | None = None) -> None:
        self._existing = existing
        self.replaces: list[dict] = []
        # When set, replace_item raises this (simulating a delete or ETag race
        # between the read and the conditional write).
        self.replace_error: Exception | None = None

    async def read_item(self, *, item: str, partition_key: str):
        if self._existing is None:
            raise CosmosResourceNotFoundError(message="missing")
        return self._existing

    async def replace_item(self, *, item, body, etag=None, match_condition=None):
        if self.replace_error is not None:
            raise self.replace_error
        self.replaces.append(body)
        return body


def _repo(existing: dict | None = None) -> tuple[CosmosDocumentLibraryRepository, _FakeDocs]:
    repo = object.__new__(CosmosDocumentLibraryRepository)
    fake = _FakeDocs(existing)
    repo._docs = fake
    return repo, fake


def _doc(user: str = "alice", **kw) -> UserDocument:
    base = dict(userId=user, filename="f.pdf")
    base.update(kw)
    return UserDocument(**base)


async def test_update_missing_id_raises_and_does_not_create():
    repo, fake = _repo(existing=None)
    doc = _doc()
    with pytest.raises(DocumentNotFoundError):
        await repo.update_document(doc)
    assert fake.replaces == []  # never resurrected


async def test_update_cross_user_raises_and_does_not_write():
    doc = _doc(user="alice")
    # Stored item is owned by someone else (defense in depth beyond the PK).
    repo, fake = _repo(existing={"id": doc.id, "userId": "mallory", "_etag": "e1"})
    with pytest.raises(DocumentNotFoundError):
        await repo.update_document(doc)
    assert fake.replaces == []


async def test_update_deleted_between_read_and_replace_raises():
    # The read succeeds but the conditional replace 404s — a delete landed in the
    # TOCTOU window. The repo must treat this as gone (raise), never resurrect.
    doc = _doc(user="alice")
    repo, fake = _repo(existing={"id": doc.id, "userId": "alice", "_etag": "e1"})
    fake.replace_error = CosmosResourceNotFoundError(message="deleted")
    with pytest.raises(DocumentNotFoundError):
        await repo.update_document(doc)


async def test_update_etag_conflict_raises():
    # The item was modified (ETag moved) between read and replace — precondition
    # failed. Treat as gone for enrich's purposes (do not clobber newer state).
    doc = _doc(user="alice")
    repo, fake = _repo(existing={"id": doc.id, "userId": "alice", "_etag": "e1"})
    fake.replace_error = CosmosAccessConditionFailedError(message="etag")
    with pytest.raises(DocumentNotFoundError):
        await repo.update_document(doc)


async def test_update_existing_owned_succeeds_via_replace():
    doc = _doc(user="alice", summary="before")
    repo, fake = _repo(existing={"id": doc.id, "userId": "alice", "_etag": "e1"})
    doc.summary = "after"
    saved = await repo.update_document(doc)
    assert saved.summary == "after"
    assert len(fake.replaces) == 1
    assert fake.replaces[0]["userId"] == "alice"


async def test_in_memory_update_missing_id_raises():
    # The other half of the parity contract, asserted directly.
    repo = InMemoryDocumentLibraryRepository()
    with pytest.raises(DocumentNotFoundError):
        await repo.update_document(_doc(user="alice"))


# --- sharing-lookup parity (Phase 11F): the Cosmos repo issues the right
# cross-partition query and marshals rows into UserDocument. A fake container
# captures the query/params and yields preset rows (Cosmos's SQL engine isn't
# exercised here — only the repo's query construction + result marshalling). ---
class _QueryDocs:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.last_query: str | None = None
        self.last_params: list | None = None

    async def query_items(self, *, query, parameters=None):
        self.last_query = query
        self.last_params = parameters
        for row in self._rows:
            yield row


def _query_repo(rows: list[dict]) -> tuple[CosmosDocumentLibraryRepository, _QueryDocs]:
    repo = object.__new__(CosmosDocumentLibraryRepository)
    fake = _QueryDocs(rows)
    repo._docs = fake
    return repo, fake


async def test_cosmos_list_shared_with_marshals_and_scopes():
    doc = _doc(user="alice")
    repo, fake = _query_repo([doc.model_dump(mode="json")])
    out = await repo.list_shared_with("BOB@Example.com ")
    assert [d.id for d in out] == [doc.id]
    # Query is scoped to shared + the normalized grantee email.
    assert "ARRAY_CONTAINS(c.acl, @email)" in fake.last_query
    by_name = {p["name"]: p["value"] for p in fake.last_params}
    assert by_name["@email"] == "bob@example.com"
    assert by_name["@shared"] == "shared"


async def test_cosmos_list_shared_with_blank_skips_query():
    repo, fake = _query_repo([_doc().model_dump(mode="json")])
    assert await repo.list_shared_with("") == []
    assert fake.last_query is None  # short-circuited, no cross-partition scan


async def test_cosmos_get_by_id_returns_first_or_none():
    doc = _doc(user="carol")
    repo, fake = _query_repo([doc.model_dump(mode="json")])
    got = await repo.get_by_id(doc.id)
    assert got is not None and got.userId == "carol"
    assert "c.id = @id" in fake.last_query

    empty, _ = _query_repo([])
    assert await empty.get_by_id("anything") is None
    blank, blank_fake = _query_repo([doc.model_dump(mode="json")])
    assert await blank.get_by_id("") is None
    assert blank_fake.last_query is None
