"""Blob storage for the document library.

Raw uploads and parsed artifacts live under ``{userId}/{documentId}/{name}`` in a
single private container. Reached ONLY through the api managed identity (AAD) — the
browser never receives a blob URL. A :class:`BlobStore` protocol lets the ingest
path stay storage-agnostic: an in-memory store backs local/dev + unit tests, and
:class:`AzureBlobStore` (lazy SDK import) backs deployments.

The ``userId`` prefix is the per-user isolation boundary at the storage tier,
mirroring the Cosmos partition key and the pgvector row filter.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Canonical artifact names within a document's prefix.
RAW_NAME = "original"
PARSED_NAME = "parsed.md"
CHUNKS_NAME = "chunks.jsonl"
# Audio/video deep-link timeline: per-segment scene/keyframe markers
# extracted from the CU result, served to the media player. Optional sidecar —
# absent for documents and for AV without scene detail.
MEDIA_NAME = "media.json"
# Subdirectory under a document's prefix holding "adjust & return" exports
# Each export writes ``{userId}/{documentId}/versions/{n}/{name}``,
# leaving the original raw/parsed artifacts immutable.
VERSIONS_DIR = "versions"


class BlobNotFoundError(Exception):
    """Raised when a blob path does not exist."""


def document_prefix(user_id: str, document_id: str) -> str:
    return f"{user_id}/{document_id}/"


def blob_path(user_id: str, document_id: str, name: str) -> str:
    return f"{document_prefix(user_id, document_id)}{name}"


def version_prefix(user_id: str, document_id: str, n: int) -> str:
    return f"{document_prefix(user_id, document_id)}{VERSIONS_DIR}/{n}/"


def version_path(user_id: str, document_id: str, n: int, name: str) -> str:
    return f"{version_prefix(user_id, document_id, n)}{name}"


@runtime_checkable
class BlobStore(Protocol):
    async def put(self, path: str, data: bytes, content_type: str | None = None) -> str: ...

    async def get(self, path: str) -> bytes: ...

    async def delete_prefix(self, prefix: str) -> int: ...

    async def close(self) -> None: ...


class InMemoryBlobStore:
    """Process-local blob store for local/dev + tests (no isolation guarantees
    beyond the path prefix; not durable)."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    async def put(self, path: str, data: bytes, content_type: str | None = None) -> str:
        self._data[path] = bytes(data)
        return path

    async def get(self, path: str) -> bytes:
        try:
            return self._data[path]
        except KeyError:
            raise BlobNotFoundError(path) from None

    async def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in self._data if k.startswith(prefix)]
        for k in keys:
            del self._data[k]
        return len(keys)

    async def close(self) -> None:
        return None


class AzureBlobStore:
    """Durable blob store on an Azure Storage account (AAD; no account keys).

    ``BlobServiceClient`` + ``DefaultAzureCredential`` are imported lazily so the
    app and tests run without the azure-storage-blob package or a live account.
    The container is provisioned by infra; ``put`` overwrites, ``delete_prefix``
    lists-and-deletes a document's artifacts.
    """

    def __init__(
        self,
        account_url: str,
        container: str,
        *,
        service_client: Any | None = None,
        credential: Any | None = None,
    ) -> None:
        self._account_url = account_url
        self._container = container
        self._service = service_client
        self._owns_service = service_client is None
        self._credential = credential
        self._owns_credential = credential is None

    def _service_client(self) -> Any:
        if self._service is None:
            from azure.identity.aio import DefaultAzureCredential
            from azure.storage.blob.aio import BlobServiceClient

            if self._credential is None:
                self._credential = DefaultAzureCredential()
            self._service = BlobServiceClient(
                account_url=self._account_url, credential=self._credential
            )
        return self._service

    def _blob_client(self, path: str) -> Any:
        return self._service_client().get_blob_client(container=self._container, blob=path)

    async def put(self, path: str, data: bytes, content_type: str | None = None) -> str:
        from azure.storage.blob import ContentSettings

        settings = ContentSettings(content_type=content_type) if content_type else None
        blob = self._blob_client(path)
        await blob.upload_blob(data, overwrite=True, content_settings=settings)
        return path

    async def get(self, path: str) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError

        blob = self._blob_client(path)
        try:
            stream = await blob.download_blob()
            return await stream.readall()
        except ResourceNotFoundError:
            raise BlobNotFoundError(path) from None

    async def delete_prefix(self, prefix: str) -> int:
        container = self._service_client().get_container_client(self._container)
        deleted = 0
        async for blob in container.list_blobs(name_starts_with=prefix):
            try:
                await container.delete_blob(blob.name)
                deleted += 1
            except Exception:  # noqa: BLE001 - best-effort purge
                logger.warning("blob delete failed path=%s", blob.name, exc_info=True)
        return deleted

    async def close(self) -> None:
        if self._owns_service and self._service is not None:
            await self._service.close()
        if self._owns_credential and self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                await close()
