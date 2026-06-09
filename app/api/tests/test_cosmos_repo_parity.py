"""Cosmos repo parity (Phase 11A/11B): ``update_document`` must raise on a
missing or cross-user id rather than silently create/resurrect it via
``upsert_item`` — matching the in-memory repo contract that the 11B ingest worker
relies on when driving status transitions (stored -> ready/failed).

The Cosmos client + AAD credential are constructed in ``__init__``; we bypass it
with ``object.__new__`` and inject a minimal async fake container so the parity
logic is exercised without any live network or managed identity.
"""
from __future__ import annotations

import pytest
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from ai4ia_api.library.cosmos_repo import CosmosDocumentLibraryRepository
from ai4ia_api.library.models import UserDocument
from ai4ia_api.library.repository import DocumentNotFoundError


class _FakeDocs:
    """Minimal async stand-in for a Cosmos container client."""

    def __init__(self, existing: dict | None = None) -> None:
        self._existing = existing
        self.upserts: list[dict] = []

    async def read_item(self, *, item: str, partition_key: str):
        if self._existing is None:
            raise CosmosResourceNotFoundError(message="missing")
        return self._existing

    async def upsert_item(self, body):
        self.upserts.append(body)
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
    assert fake.upserts == []  # never resurrected via upsert


async def test_update_cross_user_raises_and_does_not_write():
    doc = _doc(user="alice")
    # Stored item is owned by someone else (defense in depth beyond the PK).
    repo, fake = _repo(existing={"id": doc.id, "userId": "mallory"})
    with pytest.raises(DocumentNotFoundError):
        await repo.update_document(doc)
    assert fake.upserts == []


async def test_update_existing_owned_succeeds():
    doc = _doc(user="alice", summary="before")
    repo, fake = _repo(existing={"id": doc.id, "userId": "alice"})
    doc.summary = "after"
    saved = await repo.update_document(doc)
    assert saved.summary == "after"
    assert len(fake.upserts) == 1
    assert fake.upserts[0]["userId"] == "alice"
