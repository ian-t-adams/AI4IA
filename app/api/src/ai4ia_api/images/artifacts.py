"""Durable storage for generated images (Phase 11F).

A generated image is too large to flow back through the agent tool-result channel
(the runtime caps a tool result at 8 KB, and a 1024² PNG is ~1–3 MB base64), so
the ``generate_image`` tool persists the bytes here and returns only a small
reference. The bytes are served back to the browser through an authenticated
endpoint (``GET /api/images/artifacts/{id}``), never a public blob URL.

Layout mirrors the document library's per-user isolation: every artifact lives
under ``{userId}/generated/{artifactId}.png``. The ``userId`` prefix is the
storage-tier isolation boundary — the serve endpoint composes the path from the
*authenticated* user's id, so one user can never read another's artifact even by
guessing an id.
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

GENERATED_DIR = "generated"
IMAGE_CONTENT_TYPE = "image/png"
IMAGE_EXT = "png"

__all__ = [
    # Re-exported so callers can catch a missing artifact without importing the
    # library blob module directly.
    "BlobNotFoundError",
    "ImageArtifactStore",
    "artifact_path",
    "build_image_blob_store",
]


def artifact_path(user_id: str, artifact_id: str) -> str:
    """Storage path for one generated image, scoped to its owner."""
    return f"{user_id}/{GENERATED_DIR}/{artifact_id}.{IMAGE_EXT}"


def build_image_blob_store(settings: Settings) -> BlobStore:
    """Durable :class:`AzureBlobStore` when configured, else an in-memory store.

    Mirrors the document library's blob factory: a deployment supplies
    ``image_blob_account_url`` (+ container) and gets durable, AAD-only storage;
    local/dev + tests fall back to a process-local store with no extra config.
    """
    if settings.image_blob_account_url:
        return AzureBlobStore(
            settings.image_blob_account_url, settings.image_blob_container
        )
    return InMemoryBlobStore()


class ImageArtifactStore:
    """Persists and reads generated-image bytes, scoped to the owning user."""

    def __init__(self, blob: BlobStore) -> None:
        self._blob = blob

    async def put(self, user_id: str, artifact_id: str, data: bytes) -> str:
        """Store ``data`` as the user's artifact ``artifact_id``; return its path."""
        return await self._blob.put(
            artifact_path(user_id, artifact_id), data, IMAGE_CONTENT_TYPE
        )

    async def get(self, user_id: str, artifact_id: str) -> bytes:
        """Read the user's artifact bytes; raise :class:`BlobNotFoundError` if absent."""
        return await self._blob.get(artifact_path(user_id, artifact_id))

    async def close(self) -> None:
        await self._blob.close()
