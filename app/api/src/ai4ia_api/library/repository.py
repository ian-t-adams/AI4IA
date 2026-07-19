"""DocumentLibraryRepository protocol and shared errors.

Every method takes the authenticated ``user_id`` and MUST enforce ownership.
Documents and custom analyzers are partitioned by ``/userId``; ownership is still
checked explicitly (defense in depth) so a bug in partition routing can never
leak another user's data.

Built-in analyzers are not stored — :meth:`list_analyzers` merges the in-process
:data:`library.models.BUILTIN_ANALYZERS` with the user's custom registry.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .models import Analyzer, DocumentStatus, UserDocument


class DocumentNotFoundError(Exception):
    """Raised when a library document does not exist or is not owned by the user."""


class DocumentConflictError(Exception):
    """Raised when ``update_document`` loses an optimistic-concurrency race.

    The document still exists (unlike :class:`DocumentNotFoundError`) but was
    modified since the caller last loaded it, so the caller's write was
    rejected to avoid silently discarding the intervening change. Callers
    should reload the current document and retry if the edit still applies.
    """


class AnalyzerNotFoundError(Exception):
    """Raised when an analyzer does not exist or is not owned by the user."""


class AnalyzerConflictError(Exception):
    """Raised when a custom analyzer would collide with a built-in id."""


@runtime_checkable
class DocumentLibraryRepository(Protocol):
    # --- documents ---
    async def create_document(self, document: UserDocument) -> UserDocument: ...

    async def get_document(self, user_id: str, document_id: str) -> UserDocument: ...

    async def list_documents(self, user_id: str) -> list[UserDocument]: ...

    # Sharing lookups intentionally cross partitions: a grantee
    # finds documents owned by *others*. ``list_shared_with`` returns the ``shared``
    # documents whose ``acl`` contains ``email``; ``get_by_id`` resolves a single
    # document by id regardless of owner (callers MUST gate the result with
    # ``access.can_access`` before exposing it).
    async def list_shared_with(self, email: str) -> list[UserDocument]: ...

    async def get_by_id(self, document_id: str) -> UserDocument | None: ...

    async def list_by_status(
        self, statuses: Sequence[DocumentStatus]
    ) -> list[UserDocument]: ...

    async def update_document(self, document: UserDocument) -> UserDocument: ...

    async def patch_ingest_fields(
        self,
        document: UserDocument,
        changes: dict[str, object],
        *,
        require_status: DocumentStatus | None = None,
    ) -> UserDocument: ...

    # Durable tombstone (mirrors sessions.repository's delete-fence pattern):
    # CAS the document into "deleting" *before* any memory-forget/manifest
    # removal starts. Idempotent (a second call against an
    # already-tombstoned document is a no-op, not an error) so a duplicate
    # delete request never fails. Once visible, get_document treats the
    # document as not-found, which is what makes this a fence: it rejects
    # save_document_to_memory's own read (whether the initial load or a
    # post-write recheck) from the moment the tombstone lands, independent
    # of how far the manifest's own removal has progressed.
    async def mark_deleting(self, user_id: str, document_id: str) -> None: ...

    # Reverts an in-progress mark_deleting tombstone when a delete attempt
    # aborts before finishing (e.g. the memory-forget step failed) so the
    # document goes back to being a normal, fully visible, retryable handle
    # rather than staying invisible behind a tombstone that nothing will
    # ever finish removing. Idempotent/best-effort: a no-op if the document
    # is already gone or was never tombstoned.
    async def clear_deleting(self, user_id: str, document_id: str) -> None: ...

    async def delete_document(self, user_id: str, document_id: str) -> None: ...

    async def find_by_dedupe_key(
        self, user_id: str, content_hash: str, analyzer_id: str | None
    ) -> UserDocument | None: ...

    # --- analyzers (built-ins are merged in by the implementation) ---
    async def create_analyzer(self, analyzer: Analyzer) -> Analyzer: ...

    async def get_analyzer(self, user_id: str, analyzer_id: str) -> Analyzer: ...

    async def list_analyzers(self, user_id: str) -> list[Analyzer]: ...

    async def delete_analyzer(self, user_id: str, analyzer_id: str) -> None: ...
