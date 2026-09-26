"""Resolve an image-edit source the authenticated owner already has here.

The only sources are images this app already holds for the caller; nothing is
ever fetched from a URL:

* ``generated`` -- an ``image`` attachment (not an ``image_error``) on a message
  in **this** conversation, or one produced earlier in the same turn. The bytes
  are read from ``{authenticated userId}/generated/{id}.png``, so an id from
  another owner resolves to nothing, and an id from another of the caller's own
  conversations is refused before any read.
* ``library`` -- an **owner-only**, ready image document inside this
  conversation's library scope (``libraryDocumentIds`` is ``None`` for "all" or
  contains the id; ``[]`` admits none). Shared and tenant-public documents are
  never editable sources.

Both entry points (the ``edit_image`` tool and ``POST /api/images/edits``) call
:func:`load_edit_source` at execution time, then :func:`inspect_image` proves the
format, dimensions and container integrity of the bytes actually read.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..library.blob_store import BlobNotFoundError
from ..sessions.models import Message, MessageAttachment, Session
from .artifacts import ImageArtifactStore
from .source import MAX_EDIT_SOURCE_BYTES, ImageInfo, ImageSourceError, inspect_image, sha256_hex

ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SourceKind = Literal["generated", "library"]


class _MessageReader(Protocol):
    async def list_messages(self, user_id: str, session_id: str) -> list[Message]: ...


class _OwnedImageReader(Protocol):
    async def read_owned_image(
        self, user_id: str, document_id: str, *, max_bytes: int
    ) -> dict: ...


@dataclass(frozen=True)
class EditSourceRef:
    kind: SourceKind
    id: str


@dataclass(frozen=True)
class EditSource:
    ref: EditSourceRef
    data: bytes
    info: ImageInfo
    sha256: str
    filename: str | None = None

    def evidence(self) -> dict[str, Any]:
        """Bounded, byte-free provenance for receipts.

        The exact reference (kind + id) lives on the edited image's attachment.
        Receipts pass through the shared credential redactor, which masks any
        32+ character token (artifact ids, document ids, full SHA-256 digests),
        so the receipt records a 16-hex digest prefix that survives it and still
        correlates the exact source bytes.
        """
        return {
            "kind": self.ref.kind,
            "sha256Prefix": self.sha256[:16],
            "bytes": len(self.data),
            "contentType": self.info.content_type,
            "width": self.info.width,
            "height": self.info.height,
        }


def editable_attachment(attachment: MessageAttachment) -> bool:
    return (
        attachment.kind == "image"
        and attachment.status in (None, "complete")
        and ARTIFACT_ID_RE.fullmatch(attachment.id or "") is not None
    )


def conversation_images(
    messages: Iterable[Message], pending: Sequence[MessageAttachment] = (),
) -> list[MessageAttachment]:
    """Editable image attachments, most recent first.

    ``pending`` is the current turn's not-yet-persisted sink, which is newer than
    anything already stored.
    """
    seen: set[str] = set()
    ordered: list[MessageAttachment] = []
    persisted = [attachment for message in messages for attachment in message.attachments]
    for attachment in [*reversed(list(pending)), *reversed(persisted)]:
        if editable_attachment(attachment) and attachment.id not in seen:
            seen.add(attachment.id)
            ordered.append(attachment)
    return ordered


async def load_edit_source(
    *,
    ref: EditSourceRef | None,
    user_id: str,
    session: Session,
    repo: _MessageReader,
    image_artifacts: ImageArtifactStore,
    retrieval: _OwnedImageReader | None,
    pending: Sequence[MessageAttachment] = (),
) -> EditSource:
    """Resolve, scope-check and read one source; ``ref=None`` means the latest image."""
    if ref is None or ref.kind == "generated":
        images = conversation_images(await repo.list_messages(user_id, session.id), pending)
        if ref is None:
            if not images:
                raise ImageSourceError(404, "There is no image in this conversation to edit yet.")
            artifact_id = images[0].id
        elif ARTIFACT_ID_RE.fullmatch(ref.id or "") and ref.id in {a.id for a in images}:
            artifact_id = ref.id
        else:
            raise ImageSourceError(404, "That image is not part of this conversation.")
        try:
            data = await image_artifacts.get(user_id, artifact_id)
        except BlobNotFoundError as exc:
            raise ImageSourceError(404, "That image is no longer available.") from exc
        source_ref = EditSourceRef("generated", artifact_id)
        filename = None
    elif ref.kind == "library":
        if retrieval is None:
            raise ImageSourceError(404, "The document library is not available.")
        scope = session.libraryDocumentIds
        if scope is not None and ref.id not in scope:
            raise ImageSourceError(
                404, "That library document is not selected for this conversation."
            )
        result = await retrieval.read_owned_image(
            user_id, ref.id, max_bytes=MAX_EDIT_SOURCE_BYTES,
        )
        if "error" in result:
            raise ImageSourceError(int(result.get("status", 404)), str(result["error"]))
        data = result["data"]
        filename = str(result.get("filename") or "") or None
        source_ref = EditSourceRef("library", str(result.get("document_id") or ref.id))
    else:
        raise ImageSourceError(422, "Unsupported image source.")
    info = inspect_image(data)
    return EditSource(source_ref, data, info, sha256_hex(data), filename)
