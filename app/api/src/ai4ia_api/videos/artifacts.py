"""Read-only storage for legacy generated videos.

Retained MP4 bytes are served through ``GET /api/videos/artifacts/{id}``,
never a public blob URL. Video generation and artifact writes are retired.

Layout mirrors the generated-image store's per-user isolation: every artifact
lives under ``{userId}/generated/{artifactId}.mp4``. The ``userId`` prefix is the
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
VIDEO_CONTENT_TYPE = "video/mp4"
VIDEO_EXT = "mp4"

__all__ = [
    # Re-exported so callers can catch a missing artifact without importing the
    # library blob module directly.
    "BlobNotFoundError",
    "VideoArtifactStore",
    "artifact_path",
    "build_video_blob_store",
]


def artifact_path(user_id: str, artifact_id: str) -> str:
    """Storage path for one generated video, scoped to its owner."""
    return f"{user_id}/{GENERATED_DIR}/{artifact_id}.{VIDEO_EXT}"


def build_video_blob_store(settings: Settings) -> BlobStore:
    """Durable :class:`AzureBlobStore` when configured, else an in-memory store.

    Mirrors the generated-image blob factory: a deployment supplies
    ``video_blob_account_url`` (+ container) and gets durable, AAD-only storage;
    local/dev + tests fall back to a process-local store with no extra config.
    """
    if settings.video_blob_account_url:
        return AzureBlobStore(
            settings.video_blob_account_url, settings.video_blob_container
        )
    return InMemoryBlobStore()


class VideoArtifactStore:
    """Reads legacy generated-video bytes, scoped to the owning user."""

    def __init__(self, blob: BlobStore) -> None:
        self._blob = blob

    async def get(self, user_id: str, artifact_id: str) -> bytes:
        """Read the user's artifact bytes; raise :class:`BlobNotFoundError` if absent."""
        return await self._blob.get(artifact_path(user_id, artifact_id))

    async def close(self) -> None:
        await self._blob.close()
