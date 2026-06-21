"""Durable storage for over-cap document-processing results.

Most ``process_document`` results are small and return inline through the tool
result, but an extraction can exceed the runtime's 8 KB tool-result cap (e.g. a
full table dump as JSON). For those the tool persists the result text here and
returns only a small reference; the bytes are served back to the browser through
an authenticated endpoint (``GET /api/documents/artifacts/{id}``), never a public
blob URL.

Reuses the **document library's blob account** (``document_blob_account_url`` /
``document_blob_container``) rather than provisioning new storage — the processing
feature already rides the document-understanding flag, so its results belong with
the library. Each artifact lives under ``{userId}/processed/{artifactId}.md`` (the
``processed`` segment can never collide with a document's uuid-keyed prefix). The
``userId`` prefix is the storage-tier isolation boundary: the serve endpoint
composes the path from the *authenticated* user's id, so one user can never read
another's result even by guessing an id. Local/dev + tests fall back to a
process-local in-memory store with no extra config.
"""
from __future__ import annotations

import logging

from ..config import Settings
from ..library.blob_store import (
    AzureBlobStore,
    BlobNotFoundError,
    BlobStore,
    InMemoryBlobStore,
)

logger = logging.getLogger(__name__)

PROCESSED_DIR = "processed"
ANALYSIS_CONTENT_TYPE = "text/markdown"
ANALYSIS_EXT = "md"

__all__ = [
    # Re-exported so callers can catch a missing artifact without importing the
    # library blob module directly.
    "BlobNotFoundError",
    "DocumentArtifactStore",
    "artifact_path",
    "build_document_artifact_blob_store",
]


def artifact_path(user_id: str, artifact_id: str) -> str:
    """Storage path for one processing result, scoped to its owner."""
    return f"{user_id}/{PROCESSED_DIR}/{artifact_id}.{ANALYSIS_EXT}"


def build_document_artifact_blob_store(settings: Settings) -> BlobStore:
    """Durable :class:`AzureBlobStore` when the document blob account is
    configured, else an in-memory store.

    Reuses the document library's blob account (a deployment that enabled
    document understanding already provisioned it); local/dev + tests fall back to
    a process-local store with no extra config.
    """
    if settings.document_blob_account_url:
        return AzureBlobStore(
            settings.document_blob_account_url, settings.document_blob_container
        )
    return InMemoryBlobStore()


class DocumentArtifactStore:
    """Persists and reads processing-result text, scoped to the owning user."""

    def __init__(self, blob: BlobStore) -> None:
        self._blob = blob

    async def put(self, user_id: str, artifact_id: str, data: bytes) -> str:
        """Store ``data`` as the user's artifact ``artifact_id``; return its path."""
        return await self._blob.put(
            artifact_path(user_id, artifact_id), data, ANALYSIS_CONTENT_TYPE
        )

    async def get(self, user_id: str, artifact_id: str) -> bytes:
        """Read the user's artifact bytes; raise :class:`BlobNotFoundError` if absent."""
        return await self._blob.get(artifact_path(user_id, artifact_id))

    async def close(self) -> None:
        await self._blob.close()
