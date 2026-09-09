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
  can attach a lifecycle/TTL expiry rule to just that container. Local runs and
  tests fall back to a process-local in-memory store with no extra config.
* Bytes are keyed ``{userId}/{sessionId}/{documentId}`` — the ``userId`` prefix is
  the storage-tier isolation boundary (mirrors the library + processed-artifact
  stores). The fetch/delete path is ALWAYS recomposed from the *authenticated*
  user + session, never from a client-supplied string, so one user can never read
  another's retained file even by guessing an id.
* Legacy cleanup is best-effort. Protocol-v1 conversation cleanup uses strict,
  bounded passes and durable upload intents; an empty prefix cannot prove a
  previously started PUT has finished. The configured Blob lifecycle remains a
  separate retention policy, not a completion signal.

New retention is gated by ``inline_document_compute_enabled``. Explicit deletion
resumption can still clean previously retained bytes after that flag is disabled.
"""
from __future__ import annotations

import logging
import os
import re
from hashlib import sha256
from typing import TYPE_CHECKING

from ..config import Environment, Settings
from ..library.blob_store import (
    AzureBlobStore,
    BlobNotFoundError,
    BlobStore,
    InMemoryBlobStore,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..sessions.repository import SessionRepository

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
    for component in (user_id, session_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", component):
            raise ValueError("Invalid owner/session storage component")
    return f"{user_id}/{session_id}/"


def attachment_path(user_id: str, session_id: str, document_id: str) -> str:
    """Storage path for one retained original, scoped to its owner + session."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", document_id):
        raise ValueError("Invalid attachment storage component")
    return f"{session_prefix(user_id, session_id)}{document_id}"


def inline_attachment_storage_id(settings: Settings) -> str:
    """Bind cleanup to the configured target without persisting its URL."""
    if not settings.document_blob_account_url:
        return "local"
    target = (
        settings.document_blob_account_url.rstrip("/").lower()
        + "/" + settings.inline_attachment_blob_container
    )
    return "azure:" + sha256(target.encode("utf-8")).hexdigest()


def build_inline_attachment_blob_store(settings: Settings) -> BlobStore:
    """Durable :class:`AzureBlobStore` on a dedicated ephemeral container when the
    document blob account is configured, else a local-only in-memory store.

    Reuses the document library's blob account (a deployment that wants the inline
    feature already provisions blob storage) but a SEPARATE container so the
    short-lived bytes stay clearly apart from the durable corpus. Disabled
    deployments keep an inert in-memory store for unconditional cleanup calls;
    when the feature is enabled, only local runs may use that fallback.
    """
    if settings.document_blob_account_url:
        return AzureBlobStore(
            settings.document_blob_account_url,
            settings.inline_attachment_blob_container,
        )
    if (
        settings.env == Environment.local
        or not settings.inline_document_compute_enabled
    ):
        return InMemoryBlobStore()
    raise RuntimeError(
        "AI4IA_DOCUMENT_BLOB_ACCOUNT_URL is required for inline attachment "
        "retention outside local."
    )


class EphemeralAttachmentStore:
    """Retains/serves/purges inline-attachment original bytes, owner+session scoped.

    Legacy purge methods are best-effort. ``reconcile_session`` is deliberately
    strict and must not reuse their success-shaped fallback. Production reads
    recheck the canonical attachment/parent even from an already-created tool
    closure. Standalone stores without a repository are for isolated local tests.
    """

    def __init__(
        self, blob: BlobStore, *, storage_id: str = "local",
        session_repo: SessionRepository | None = None,
    ) -> None:
        self._blob = blob
        self.storage_id = storage_id
        self._session_repo = session_repo

    async def reconcile_session(self, user_id: str, session_id: str, *, limit: int) -> bool:
        """Strict cleanup used only by an explicitly requested deletion pass."""
        return await self._blob.delete_prefix_page(
            session_prefix(user_id, session_id), limit=limit
        )

    async def put(
        self,
        user_id: str,
        session_id: str,
        document_id: str,
        data: bytes,
        content_type: str | None = None,
        *,
        single_attempt: bool = False,
    ) -> str:
        """Retain ``data`` as the original for one attachment; return its blob path."""
        if single_attempt:
            # Ticketed v1 uploads cannot acknowledge a retry while an earlier
            # timed-out attempt may still land. Legacy retries remain unchanged.
            return await self._blob.put(
                attachment_path(user_id, session_id, document_id), data,
                content_type or "application/octet-stream", single_attempt=True,
            )
        return await self._blob.put(
            attachment_path(user_id, session_id, document_id),
            data,
            content_type or "application/octet-stream",
        )

    async def get(self, user_id: str, session_id: str, document_id: str) -> bytes:
        """Read one attachment's retained bytes; raise :class:`BlobNotFoundError`
        when absent (e.g. already purged or never retained)."""
        from ..sessions.repository import SessionNotFoundError

        path = attachment_path(user_id, session_id, document_id)
        if self._session_repo is not None:
            try:
                document = await self._session_repo.get_document(user_id, session_id, document_id)
            except SessionNotFoundError as exc:
                raise BlobNotFoundError(path) from exc
            if document is None or not document.rawRef:
                raise BlobNotFoundError(path)
        return await self._blob.get(path)

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
