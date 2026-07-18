"""The "adjust & return" export service.

Writes a model-produced adjustment of a *ready* library document as a **new
versioned blob** under ``{userId}/{documentId}/versions/{n}/...`` and appends a
:class:`~ai4ia_api.library.models.DocumentVersion` pointer to the manifest. The
original raw + parsed artifacts are never touched, so the source stays immutable
and every adjustment is additive.

Governance mirrors the retrieval consumer (11B-2):

* **Ownership + status gate.** Export and version download both resolve the
  document via ``library.get_document`` (raises :class:`DocumentNotFoundError`
  for a missing or cross-user id) and require ``status == ready`` — a missing,
  unowned, or non-ready document returns a structured ``{"error": ...}`` (never an
  exception, never an existence leak).
* **Bounded.** Exported content is capped at ``document_export_max_chars`` so a
  single adjustment can't write an unbounded blob.
* **Sanitized.** The stored filename is ``_safe_filename``-normalized and the
  provenance note is single-lined, so neither can carry structure back to the
  model or the UI.

All IO is injected (repository + blob store) so the service is unit-tested end to
end without network or Azure SDKs, and shares the ingestor's stores for
producer/consumer parity exactly like the retrieval service.
"""
from __future__ import annotations

import logging
import secrets

from ..config import Settings
from .blob_store import BlobNotFoundError, BlobStore, version_path
from .models import DocumentStatus, DocumentVersion, UserDocument
from .repository import DocumentLibraryRepository, DocumentNotFoundError

logger = logging.getLogger(__name__)

_NOTE_LIMIT = 200
_NAME_LIMIT = 200


def _one_line(text: str, limit: int) -> str:
    """Single-line, length-bounded text safe to store + surface."""
    return (text or "").replace("\n", " ").replace("\r", " ").strip()[:limit]


def _safe_filename(name: str | None) -> str:
    """Strip path components + non-printables and bound the length (mirrors the
    ingest producer's ``_safe_filename`` so a crafted name can never escape a
    field or forge a path)."""
    base = (name or "export.md").replace("\\", "/").split("/")[-1]
    base = "".join(c for c in base if c.isprintable()).strip()
    return (base or "export.md")[:_NAME_LIMIT]


class DocumentExportService:
    """Writes versioned "adjust & return" artifacts and reads them back, gated to
    a user's *ready* library. Holds no per-user state; every method takes
    ``user_id`` and is ownership- and status-gated."""

    def __init__(
        self,
        *,
        library: DocumentLibraryRepository,
        blob_store: BlobStore,
        settings: Settings,
    ) -> None:
        self._library = library
        self._blob = blob_store
        self._settings = settings

    async def _ready_doc(self, user_id: str, document_id: str) -> tuple[UserDocument | None, dict | None]:
        """Resolve + gate a document. Returns ``(doc, None)`` when readable, or
        ``(None, error_dict)`` for a missing/cross-user/non-ready document."""
        document_id = (document_id or "").strip()
        if not document_id:
            return None, {"error": "document_id is required."}
        try:
            doc = await self._library.get_document(user_id, document_id)
        except DocumentNotFoundError:
            return None, {"error": f"No document found with id '{document_id}'."}
        except Exception:  # noqa: BLE001 - degrade, never propagate
            logger.warning(
                "export gate load failed user=%s id=%s", user_id, document_id, exc_info=True
            )
            return None, {"error": "Could not access that document right now."}
        if doc.status != DocumentStatus.ready:
            safe_name = _safe_filename(doc.filename)
            return None, {
                "error": (
                    f"Document '{safe_name}' is not ready (status="
                    f"{doc.status.value}); it cannot be adjusted yet."
                )
            }
        return doc, None

    async def export_version(
        self,
        user_id: str,
        document_id: str,
        *,
        content: str,
        filename: str | None = None,
        content_type: str = "text/markdown",
        note: str = "",
    ) -> dict:
        """Write ``content`` as a new version of a ready document and append it to
        the manifest. Returns a structured result with the new version number, or
        ``{"error": ...}`` for a missing/cross-user/non-ready document or empty
        content. The original artifacts are never modified."""
        doc, err = await self._ready_doc(user_id, document_id)
        if err is not None:
            return err
        assert doc is not None  # for type-checkers; err-None implies doc set

        body = content or ""
        cap = max(1, self._settings.document_export_max_chars)
        truncated = len(body) > cap
        body = body[:cap]
        if not body.strip():
            return {"error": "Refusing to export empty content."}

        safe_name = _safe_filename(filename)
        safe_note = _one_line(note, _NOTE_LIMIT)
        n = doc.next_version
        # A random per-attempt token (not just n) keys the blob path: two
        # concurrent exports of the same document both read next_version
        # before either commits, so they can compute the same n. Without a
        # unique path per attempt, the loser's cleanup below could delete the
        # winner's already-committed blob (see version_path's docstring).
        token = secrets.token_hex(8)
        path = version_path(user_id, document_id, n, token, safe_name)
        data = body.encode("utf-8")
        try:
            await self._blob.put(path, data, content_type or "text/markdown")
        except Exception:  # noqa: BLE001 - degrade, never propagate
            logger.warning(
                "export blob write failed user=%s id=%s n=%s",
                user_id, document_id, n, exc_info=True,
            )
            return {"error": "Could not save the adjusted document right now."}

        version = DocumentVersion(
            n=n,
            path=path,
            filename=safe_name,
            contentType=content_type or "text/markdown",
            size=len(data),
            note=safe_note,
        )
        async def _cleanup_orphan_blob() -> None:
            # Best-effort purge of the version blob just written above so a
            # manifest write failure (of any kind) never leaves an
            # un-referenced artifact behind.
            try:
                await self._blob.delete_prefix(path)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.warning("orphan version cleanup failed path=%s", path, exc_info=True)

        # Re-read + append under the manifest's own update path so we never blindly
        # overwrite a racing change; update_document raises on a vanished doc
        # (no create-on-missing).
        doc.versions.append(version)
        try:
            await self._library.update_document(doc)
        except DocumentNotFoundError:
            # The document was deleted between the gate and the manifest write.
            await _cleanup_orphan_blob()
            return {"error": f"No document found with id '{document_id}'."}
        except Exception:  # noqa: BLE001 - degrade, never propagate
            logger.warning(
                "export manifest update failed user=%s id=%s n=%s",
                user_id, document_id, n, exc_info=True,
            )
            await _cleanup_orphan_blob()
            return {"error": "Could not record the adjusted document right now."}

        return {
            "document_id": document_id,
            "version": n,
            "filename": safe_name,
            "size": len(data),
            "note": safe_note,
            "truncated": truncated,
        }

    async def list_versions(self, user_id: str, document_id: str) -> dict:
        """List the versions of a ready document (sanitized), or an error."""
        doc, err = await self._ready_doc(user_id, document_id)
        if err is not None:
            return err
        assert doc is not None
        items = [
            {
                "version": v.n,
                "filename": _safe_filename(v.filename),
                "contentType": v.contentType,
                "size": v.size,
                "note": _one_line(v.note, _NOTE_LIMIT),
                "createdAt": v.createdAt.isoformat(),
            }
            for v in sorted(doc.versions, key=lambda v: v.n)
        ]
        return {"document_id": document_id, "versions": items}

    async def read_version(self, user_id: str, document_id: str, n: int) -> dict:
        """Read a version's bytes for download, ownership- + status-gated.

        Returns ``{"filename","contentType","data"}`` or ``{"error": ...}``. A
        missing/cross-user/non-ready document or unknown version number yields a
        generic not-found (never leaks existence)."""
        doc, err = await self._ready_doc(user_id, document_id)
        if err is not None:
            return err
        assert doc is not None
        version = next((v for v in doc.versions if v.n == n), None)
        if version is None:
            return {"error": f"No version {n} for document '{document_id}'."}
        try:
            data = await self._blob.get(version.path)
        except BlobNotFoundError:
            return {"error": f"No content stored for version {n}."}
        except Exception:  # noqa: BLE001 - degrade, never propagate
            logger.warning(
                "version read failed user=%s id=%s n=%s",
                user_id, document_id, n, exc_info=True,
            )
            return {"error": "Could not read that version right now."}
        return {
            "filename": _safe_filename(version.filename),
            "contentType": version.contentType,
            "data": data,
        }
