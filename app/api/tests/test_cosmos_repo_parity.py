"""Cosmos repo parity for document library: ``update_document`` must raise on a
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

from datetime import datetime, timedelta, timezone

import pytest
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceNotFoundError,
)

from ai4ia_api.library.cosmos_repo import CosmosDocumentLibraryRepository
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.library.models import (
    DocumentAnalysis,
    DocumentStatus,
    UserDocument,
)
from ai4ia_api.library.repository import DocumentConflictError, DocumentNotFoundError


class _FakeDocs:
    """Minimal async stand-in for a Cosmos container client."""

    def __init__(self, existing: dict | None = None) -> None:
        self._existing = existing
        self.replaces: list[dict] = []
        # When set, replace_item raises this (simulating a delete or ETag race
        # between the read and the conditional write).
        self.replace_error: Exception | None = None
        # Every etag actually sent as the replace_item precondition, in call
        # order — lets tests assert *which* etag (caller's own vs. a fresh
        # re-read) was used without re-implementing Cosmos's own CAS matching.
        self.replace_etags: list[str | None] = []
        # The etag Cosmos would assign the item after a successful replace
        # (real Cosmos always returns fresh system properties in the response
        # body). Defaults to echoing the write etag unchanged.
        self.replace_response_etag: str | None = None
        self.patch_conflicts = 0
        self.patch_etags: list[str | None] = []

    async def read_item(self, *, item: str, partition_key: str):
        if self._existing is None:
            raise CosmosResourceNotFoundError(message="missing")
        return self._existing

    async def replace_item(self, *, item, body, etag=None, match_condition=None):
        self.replace_etags.append(etag)
        if self.replace_error is not None:
            raise self.replace_error
        self.replaces.append(body)
        response_etag = self.replace_response_etag if self.replace_response_etag is not None else etag
        return {**body, "_etag": response_etag}

    async def patch_item(
        self,
        *,
        item,
        partition_key,
        patch_operations,
        etag=None,
        match_condition=None,
    ):
        assert len(patch_operations) <= 10
        self.patch_etags.append(etag)
        if self.patch_conflicts:
            self.patch_conflicts -= 1
            assert self._existing is not None
            self._existing = {
                **self._existing,
                "visibility": "private",
                "acl": [],
                "_etag": "e2",
            }
            raise CosmosAccessConditionFailedError(message="etag")
        assert self._existing is not None
        for operation in patch_operations:
            path = operation["path"].lstrip("/")
            if operation["op"] == "remove":
                self._existing.pop(path, None)
            else:
                self._existing[path] = operation["value"]
        self._existing["_etag"] = "e3"
        return self._existing


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


async def test_update_etag_conflict_raises_conflict_not_not_found():
    # The item was modified (ETag moved) between read and replace — precondition
    # failed. This is a genuine conflict, not a not-found: the document still
    # exists, so a 404-shaped error here would be a false negative (production
    # finding — a caller previously saw a false "document not found" for a
    # document that was very much still there).
    doc = _doc(user="alice")
    repo, fake = _repo(existing={"id": doc.id, "userId": "alice", "_etag": "e1"})
    fake.replace_error = CosmosAccessConditionFailedError(message="etag")
    with pytest.raises(DocumentConflictError):
        await repo.update_document(doc)


async def test_update_existing_owned_succeeds_via_replace():
    doc = _doc(user="alice", summary="before")
    repo, fake = _repo(existing={"id": doc.id, "userId": "alice", "_etag": "e1"})
    doc.summary = "after"
    saved = await repo.update_document(doc)
    assert saved.summary == "after"
    assert len(fake.replaces) == 1
    assert fake.replaces[0]["userId"] == "alice"


async def test_update_document_write_precondition_uses_callers_own_etag():
    """The write precondition must be the *caller's own* etag — captured when
    they loaded the document — not a freshly re-read one. Re-reading right
    before the write would always happen to match, silently discarding any
    edit that landed between the caller's load and this call (e.g. two
    concurrent annotation adds on the same document). Give the caller's doc a
    stale etag and the point-read a *different*, newer one: the stale one
    must be what actually gets sent as the precondition."""
    doc = _doc(user="alice", summary="before")
    doc._etag = "caller-stale-etag"
    repo, fake = _repo(
        existing={"id": doc.id, "userId": "alice", "_etag": "fresh-current-etag"}
    )
    doc.summary = "after"
    await repo.update_document(doc)
    assert fake.replace_etags == ["caller-stale-etag"]


async def test_update_document_rejects_write_when_callers_etag_is_stale():
    # The other half of the same fix, from the caller's point of view: if the
    # underlying store's current etag has moved on since the caller loaded
    # the document (simulated here via replace_error, since the fake does not
    # itself implement CAS matching), the update must lose rather than
    # clobber whatever changed it — and the caller must see a conflict, not a
    # false "not found".
    doc = _doc(user="alice", summary="before")
    doc._etag = "caller-stale-etag"
    repo, fake = _repo(
        existing={"id": doc.id, "userId": "alice", "_etag": "fresh-current-etag"}
    )
    fake.replace_error = CosmosAccessConditionFailedError(message="etag")
    doc.summary = "after"
    with pytest.raises(DocumentConflictError):
        await repo.update_document(doc)
    assert fake.replace_etags == ["caller-stale-etag"]


async def test_update_document_falls_back_to_fresh_etag_when_caller_has_none():
    # A document built without ever being loaded has no etag of its own to
    # defend; it still gets the pre-fix protection of a conditional write
    # using the just-read current etag.
    doc = _doc(user="alice", summary="before")
    assert doc._etag is None
    repo, fake = _repo(existing={"id": doc.id, "userId": "alice", "_etag": "e1"})
    doc.summary = "after"
    await repo.update_document(doc)
    assert fake.replace_etags == ["e1"]


async def test_update_document_refreshes_etag_from_replace_response():
    # After a successful write, the returned document's etag must reflect the
    # *new* value Cosmos assigned, not the (now stale) precondition value —
    # otherwise a caller chaining a second update on the same object would
    # spuriously conflict against its own prior write.
    doc = _doc(user="alice", summary="before")
    doc._etag = "e1"
    repo, fake = _repo(existing={"id": doc.id, "userId": "alice", "_etag": "e1"})
    fake.replace_response_etag = "e2"
    doc.summary = "after"
    saved = await repo.update_document(doc)
    assert saved._etag == "e2"


async def test_in_memory_update_missing_id_raises():
    # The other half of the parity contract, asserted directly.
    repo = InMemoryDocumentLibraryRepository()
    with pytest.raises(DocumentNotFoundError):
        await repo.update_document(_doc(user="alice"))


async def test_in_memory_update_rejects_write_when_callers_etag_is_stale():
    # Parity with the Cosmos repo: a write built from a document whose etag no
    # longer matches the stored one must be rejected as a conflict, not
    # silently applied (which would clobber whatever changed it in between).
    repo = InMemoryDocumentLibraryRepository()
    created = await repo.create_document(_doc(user="alice", summary="before"))
    stale = created.model_copy(deep=True)
    stale._etag = created._etag
    # Someone else updates the document first, advancing its stored etag.
    other_edit = created.model_copy(deep=True)
    other_edit._etag = created._etag
    other_edit.summary = "edited by someone else"
    await repo.update_document(other_edit)
    # The caller's copy is now stale relative to the stored document.
    stale.summary = "after"
    with pytest.raises(DocumentConflictError):
        await repo.update_document(stale)


async def test_update_document_parity_conflict_vs_not_found_across_backends():
    """Direct side-by-side parity check requested in review: both backends
    must raise the *same* domain errors for the *same* scenarios — a missing
    document is always ``DocumentNotFoundError`` (never resurrected), and a
    write built from a stale load is always ``DocumentConflictError`` (never
    silently applied, and never misreported as a not-found)."""
    memory_repo = InMemoryDocumentLibraryRepository()
    cosmos_repo, fake = _repo(existing=None)

    missing_doc = _doc(user="alice")
    with pytest.raises(DocumentNotFoundError):
        await memory_repo.update_document(missing_doc)
    with pytest.raises(DocumentNotFoundError):
        await cosmos_repo.update_document(missing_doc)

    created = await memory_repo.create_document(_doc(user="alice", summary="before"))
    stale = created.model_copy(deep=True)
    other_edit = created.model_copy(deep=True)
    other_edit.summary = "edited concurrently"
    await memory_repo.update_document(other_edit)
    stale.summary = "after"
    with pytest.raises(DocumentConflictError):
        await memory_repo.update_document(stale)

    cosmos_repo, fake = _repo(
        existing={"id": stale.id, "userId": "alice", "_etag": "fresh-current-etag"}
    )
    stale._etag = "caller-stale-etag"
    fake.replace_error = CosmosAccessConditionFailedError(message="etag")
    with pytest.raises(DocumentConflictError):
        await cosmos_repo.update_document(stale)


async def test_ingest_patch_retries_cas_without_restoring_revoked_acl():
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    doc = _doc(user="alice", updatedAt=old)
    existing = {
        **doc.model_dump(mode="json"),
        "visibility": "shared",
        "acl": ["bob@example.com"],
        "_etag": "e1",
    }
    repo, fake = _repo(existing=existing)
    doc._etag = "e1"
    fake.patch_conflicts = 1
    saved = await repo.patch_ingest_fields(
        doc,
        {
            "status": DocumentStatus.ready,
            "chunkCount": 2,
            "analysis": DocumentAnalysis(
                provider="mistral",
                model="mistral-document-ai-2512",
                pages=1,
            ),
        },
    )
    assert fake.patch_etags == ["e1", "e2"]
    assert saved.status == DocumentStatus.ready
    assert saved.visibility.value == "private"
    assert saved.acl == []
    assert saved.analysis is not None
    assert saved.analysis.provider == "mistral"
    assert isinstance(fake._existing["analysis"], dict)
    assert saved.updatedAt > old


async def test_ingest_patch_rejects_more_than_cosmos_ten_operation_limit():
    doc = _doc(user="alice")
    repo, _fake = _repo(existing={**doc.model_dump(mode="json"), "_etag": "e1"})
    doc._etag = "e1"

    with pytest.raises(ValueError, match="10-operation limit"):
        await repo.patch_ingest_fields(
            doc, {f"field{index}": index for index in range(10)}
        )


# --- sharing-lookup parity: the Cosmos repo issues the right
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
