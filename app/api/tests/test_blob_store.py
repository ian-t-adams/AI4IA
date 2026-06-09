"""In-memory blob store + path helpers (Phase 11B)."""
from __future__ import annotations

import pytest

from ai4ia_api.library.blob_store import (
    BlobNotFoundError,
    InMemoryBlobStore,
    blob_path,
    document_prefix,
)


def test_path_helpers():
    assert document_prefix("u1", "d1") == "u1/d1/"
    assert blob_path("u1", "d1", "original.pdf") == "u1/d1/original.pdf"


async def test_put_then_get_roundtrip():
    store = InMemoryBlobStore()
    path = blob_path("u1", "d1", "parsed.md")
    returned = await store.put(path, b"# hi", "text/markdown")
    assert returned == path
    assert await store.get(path) == b"# hi"


async def test_get_missing_raises():
    store = InMemoryBlobStore()
    with pytest.raises(BlobNotFoundError):
        await store.get("u1/d1/none")


async def test_delete_prefix_removes_only_matching():
    store = InMemoryBlobStore()
    await store.put("u1/d1/a", b"1")
    await store.put("u1/d1/b", b"2")
    await store.put("u1/d2/c", b"3")
    deleted = await store.delete_prefix(document_prefix("u1", "d1"))
    assert deleted == 2
    assert await store.get("u1/d2/c") == b"3"
    with pytest.raises(BlobNotFoundError):
        await store.get("u1/d1/a")
