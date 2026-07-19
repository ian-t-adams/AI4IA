"""In-memory blob store + path helpers."""
from __future__ import annotations

import logging

import pytest

from ai4ia_api.library.blob_store import (
    AzureBlobStore,
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


class _FakeBlobItem:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeContainerClient:
    """Minimal stand-in for azure.storage.blob.aio.ContainerClient, just
    enough of list_blobs/delete_blob for AzureBlobStore.delete_prefix."""

    def __init__(self, names: list[str], *, raise_for: dict[str, Exception] | None = None) -> None:
        self._names = names
        self._raise_for = raise_for or {}
        self.deleted: list[str] = []

    async def list_blobs(self, *, name_starts_with: str):
        for name in self._names:
            if name.startswith(name_starts_with):
                yield _FakeBlobItem(name)

    async def delete_blob(self, name: str) -> None:
        if name in self._raise_for:
            raise self._raise_for[name]
        self.deleted.append(name)


class _FakeServiceClient:
    def __init__(self, container_client: _FakeContainerClient) -> None:
        self._container_client = container_client

    def get_container_client(self, container: str) -> _FakeContainerClient:
        return self._container_client


def _azure_store(container: _FakeContainerClient) -> AzureBlobStore:
    # Injecting service_client bypasses AzureBlobStore's lazy
    # azure-storage-blob/azure-identity imports entirely, so this works
    # without either package installed.
    return AzureBlobStore(
        "https://example.blob.core.windows.net",
        "docs",
        service_client=_FakeServiceClient(container),
    )


async def test_azure_delete_prefix_deletes_only_matching_blobs():
    container = _FakeContainerClient(["u1/d1/a", "u1/d1/b", "u1/d2/c"])
    store = _azure_store(container)
    deleted = await store.delete_prefix("u1/d1/")
    assert deleted == 2
    assert container.deleted == ["u1/d1/a", "u1/d1/b"]


class _HostileDeleteError(RuntimeError):
    """Stands in for a real storage SDK error whose own message can embed
    the full blob URL/path - the fix must not rely on that string staying
    safe just because the log's format-string argument was sanitized."""


async def test_azure_delete_prefix_log_never_contains_blob_name_or_raw_exception(caplog):
    """MEDIUM finding (round 6): AzureBlobStore.delete_prefix logged the raw
    blob.name - whose final segment can be a model/user-supplied export
    filename, see version_path - plus exc_info=True, whose rendered
    traceback reproduces the exception's own message (which for real storage
    SDK errors can itself embed the full request path/URL, e.g. a 404's
    resource-not-found detail). Only the trimmed path and the exception's
    type name are safe to log; neither the filename nor the raw exception
    text may appear."""
    sensitive_name = "super-secret-quarterly-plan-do-not-log.pdf"
    hostile_path = f"u1/d1/versions/3/tok123/{sensitive_name}"
    hostile_exc = _HostileDeleteError(
        f"blob not found at https://acct.blob.core.windows.net/docs/{hostile_path}?sig=leaked"
    )
    container = _FakeContainerClient([hostile_path], raise_for={hostile_path: hostile_exc})
    store = _azure_store(container)

    with caplog.at_level(logging.WARNING, logger="ai4ia_api.library.blob_store"):
        deleted = await store.delete_prefix("u1/d1/")

    assert deleted == 0
    assert caplog.records, "expected the delete-failure path to log something"
    message = caplog.records[0].getMessage()
    assert sensitive_name not in message
    assert "sig=leaked" not in message
    # The opaque ids/token portion of the path is still fine, and useful, to log.
    assert "u1/d1/versions/3/tok123" in message
    assert "error_type=_HostileDeleteError" in message
    # exc_info must not be attached: its rendered traceback would reproduce
    # the exception's own (here hostile) message regardless of the format
    # string's own arguments.
    assert caplog.records[0].exc_info is None
