"""In-memory DocumentLibraryRepository (Phase 11A): ownership isolation, dedupe
lookup, and the analyzer registry (built-in merge + custom CRUD + conflicts)."""
from __future__ import annotations

import pytest

from ai4ia_api.library.hashing import content_hash
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.library.models import (
    BUILTIN_ANALYZER_IDS,
    Analyzer,
    UserDocument,
)
from ai4ia_api.library.repository import (
    AnalyzerConflictError,
    AnalyzerNotFoundError,
    DocumentNotFoundError,
)


@pytest.fixture
def repo() -> InMemoryDocumentLibraryRepository:
    return InMemoryDocumentLibraryRepository()


def _doc(user="alice", **kw) -> UserDocument:
    base = dict(userId=user, filename="f.pdf")
    base.update(kw)
    return UserDocument(**base)


# --- documents ---
async def test_create_get_list_delete_document(repo):
    doc = await repo.create_document(_doc())
    got = await repo.get_document("alice", doc.id)
    assert got.id == doc.id

    listed = await repo.list_documents("alice")
    assert [d.id for d in listed] == [doc.id]

    await repo.delete_document("alice", doc.id)
    with pytest.raises(DocumentNotFoundError):
        await repo.get_document("alice", doc.id)
    assert await repo.list_documents("alice") == []


async def test_document_ownership_isolation(repo):
    doc = await repo.create_document(_doc(user="alice"))
    # Another user can neither read nor list it.
    with pytest.raises(DocumentNotFoundError):
        await repo.get_document("mallory", doc.id)
    assert await repo.list_documents("mallory") == []
    # A mallory delete must not remove alice's document.
    await repo.delete_document("mallory", doc.id)
    assert (await repo.get_document("alice", doc.id)).id == doc.id


async def test_delete_document_is_idempotent(repo):
    await repo.delete_document("alice", "nope")  # no raise


async def test_update_document_requires_existing(repo):
    doc = _doc()
    with pytest.raises(DocumentNotFoundError):
        await repo.update_document(doc)
    await repo.create_document(doc)
    doc.summary = "updated"
    saved = await repo.update_document(doc)
    assert saved.summary == "updated"


async def test_list_documents_newest_first(repo):
    a = await repo.create_document(_doc(filename="a"))
    b = await repo.create_document(_doc(filename="b"))
    ids = [d.id for d in await repo.list_documents("alice")]
    assert ids == [b.id, a.id]


# --- dedupe ---
async def test_find_by_dedupe_key(repo):
    h = content_hash(b"payload")
    doc = await repo.create_document(
        _doc(contentHash=h, analyzerId="builtin-document")
    )
    found = await repo.find_by_dedupe_key("alice", h, "builtin-document")
    assert found is not None and found.id == doc.id
    # Same bytes, different analyzer -> distinct result (no collision).
    assert await repo.find_by_dedupe_key("alice", h, "builtin-image") is None
    # Different user -> isolated.
    assert await repo.find_by_dedupe_key("bob", h, "builtin-document") is None


# --- analyzers ---
async def test_list_analyzers_includes_builtins(repo):
    listed = await repo.list_analyzers("alice")
    ids = {a.id for a in listed}
    assert BUILTIN_ANALYZER_IDS <= ids


async def test_custom_analyzer_crud_and_isolation(repo):
    a = await repo.create_analyzer(Analyzer(userId="alice", name="Invoices"))
    got = await repo.get_analyzer("alice", a.id)
    assert got.name == "Invoices"

    # Built-ins + the new custom analyzer are listed for the owner only.
    owner_ids = {x.id for x in await repo.list_analyzers("alice")}
    assert a.id in owner_ids
    other_ids = {x.id for x in await repo.list_analyzers("bob")}
    assert a.id not in other_ids
    assert BUILTIN_ANALYZER_IDS <= other_ids

    with pytest.raises(AnalyzerNotFoundError):
        await repo.get_analyzer("bob", a.id)

    await repo.delete_analyzer("alice", a.id)
    with pytest.raises(AnalyzerNotFoundError):
        await repo.get_analyzer("alice", a.id)


async def test_custom_analyzer_cannot_shadow_builtin(repo):
    builtin_id = next(iter(BUILTIN_ANALYZER_IDS))
    with pytest.raises(AnalyzerConflictError):
        await repo.create_analyzer(
            Analyzer(id=builtin_id, userId="alice", name="hijack")
        )


async def test_builtin_analyzer_not_deletable(repo):
    builtin_id = next(iter(BUILTIN_ANALYZER_IDS))
    with pytest.raises(AnalyzerNotFoundError):
        await repo.delete_analyzer("alice", builtin_id)
    # Still resolvable after the failed delete.
    assert (await repo.get_analyzer("alice", builtin_id)).id == builtin_id
