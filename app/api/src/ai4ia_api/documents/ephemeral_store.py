"""Ephemeral retention of inline-attachment ORIGINAL bytes (default-OFF feature).

The instant inline-attachment path (:mod:`ai4ia_api.routers.documents`)
stores only *extracted text* and discards the uploaded bytes. The inline
code-interpreter feature needs the REAL file (PDF layout / xlsx cells / image), so
when it is enabled the upload path RETAINS the original bytes here, briefly, so the
``analyze_attachment`` tool can hand them to the Code Interpreter sandbox at
analysis time.

Design (the lowest-risk mechanism that fits the codebase):

* Reuses the document library's blob account + managed-identity wiring
  (:class:`~ai4ia_api.library.blob_store.AzureBlobStore`) but writes to a SEPARATE,
  clearly-ephemeral container (``inline_attachment_blob_container``) so the
  short-lived inline bytes never mingle with the durable library corpus and infra
  can attach a lifecycle/TTL expiry rule to just that container. Local/dev + tests
  fall back to a process-local in-memory store with no extra config.
* Bytes are keyed ``{userId}/{sessionId}/{documentId}`` — the ``userId`` prefix is
  the storage-tier isolation boundary (mirrors the library + processed-artifact
  stores). The fetch/delete path is ALWAYS recomposed from the *authenticated*
  user + session, never from a client-supplied string, so one user can never read
  another's retained file even by guessing an id.
* Lifecycle: a single object is deleted when its document is deleted; the whole
  ``{userId}/{sessionId}/`` prefix is purged when the session is deleted. A blob
  lifecycle rule on the dedicated container is the durable TTL backstop.

Everything here is gated by the default-OFF ``inline_document_compute_enabled``
flag at the call sites: when the flag is off nothing in this module is ever
reached (no bytes retained, no store touched).
"""
from __future__ import annotations

import logging
import os

from ..config import Settings
from ..library.blob_store import (
    AzureBlobStore,
    BlobNotFoundError,
    BlobStore,
    InMemoryBlobStore,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BlobNotFoundError",
    "EphemeralAttachmentStore",
    "ci_supports_file",
    "build_inline_attachment_blob_store",
    "attachment_path",
    "session_prefix",
]

# File extensions the Azure OpenAI code interpreter can ingest directly (verified
# on Microsoft Learn — Responses API "Supported Files"). Mirrors the library
# compute path's allowlist; kept local so the inline feature stays self-contained.
_CI_SUPPORTED_EXTENSIONS = frozenset({
    ".c", ".cs", ".cpp", ".csv", ".doc", ".docx", ".html", ".java", ".json",
    ".md", ".pdf", ".php", ".pptx", ".py", ".rb", ".tex", ".txt", ".css", ".js",
    ".sh", ".ts", ".jpeg", ".jpg", ".gif", ".pkl", ".png", ".tar", ".xlsx",
    ".xml", ".zip",
})


def ci_supports_file(filename: str) -> bool:
    """True when the file's extension is one the code interpreter can ingest."""
    _, ext = os.path.splitext(filename or "")
    return ext.lower() in _CI_SUPPORTED_EXTENSIONS


def session_prefix(user_id: str, session_id: str) -> str:
    """Storage prefix for one session's retained originals (for prefix purges)."""
    return f"{user_id}/{session_id}/"


def attachment_path(user_id: str, session_id: str, document_id: str) -> str:
    """Storage path for one retained original, scoped to its owner + session."""
    return f"{session_prefix(user_id, session_id)}{document_id}"


def build_inline_attachment_blob_store(settings: Settings) -> BlobStore:
    """Durable :class:`AzureBlobStore` on a dedicated ephemeral container when the
    document blob account is configured, else an in-memory store.

    Reuses the document library's blob account (a deployment that wants the inline
    feature already provisions blob storage) but a SEPARATE container so the
    short-lived bytes stay clearly apart from the durable corpus; local/dev + tests
    fall back to a process-local store with no extra config.
    """
    if settings.document_blob_account_url:
        return AzureBlobStore(
            settings.document_blob_account_url,
            settings.inline_attachment_blob_container,
        )
    return InMemoryBlobStore()


class EphemeralAttachmentStore:
    """Retains/serves/purges inline-attachment original bytes, owner+session scoped.

    All methods are best-effort at the call sites (retention must never break an
    upload; cleanup must never break a delete), but the store itself surfaces
    :class:`BlobNotFoundError` from :meth:`get` so the analyze tool can distinguish
    "no retained bytes" from a transport failure.
    """

    def __init__(self, blob: BlobStore) -> None:
        self._blob = blob

    async def put(
        self,
        user_id: str,
        session_id: str,
        document_id: str,
        data: bytes,
        content_type: str | None = None,
    ) -> str:
        """Retain ``data`` as the original for one attachment; return its blob path."""
        return await self._blob.put(
            attachment_path(user_id, session_id, document_id),
            data,
            content_type or "application/octet-stream",
        )

    async def get(self, user_id: str, session_id: str, document_id: str) -> bytes:
        """Read one attachment's retained bytes; raise :class:`BlobNotFoundError`
        when absent (e.g. already purged or never retained)."""
        return await self._blob.get(attachment_path(user_id, session_id, document_id))

    async def delete(self, user_id: str, session_id: str, document_id: str) -> None:
        """Purge one attachment's retained bytes. Best-effort; never raises."""
        try:
            await self._blob.delete_prefix(
                attachment_path(user_id, session_id, document_id)
            )
        except Exception:  # noqa: BLE001 - cleanup must never break a delete
            logger.warning(
                "ephemeral attachment delete failed session=%s id=%s",
                session_id, document_id, exc_info=True,
            )

    async def delete_session(self, user_id: str, session_id: str) -> int:
        """Purge every retained original for a session. Best-effort; never raises."""
        try:
            return await self._blob.delete_prefix(session_prefix(user_id, session_id))
        except Exception:  # noqa: BLE001 - cleanup must never break a delete
            logger.warning(
                "ephemeral session purge failed session=%s", session_id, exc_info=True
            )
            return 0

    async def close(self) -> None:
        await self._blob.close()
