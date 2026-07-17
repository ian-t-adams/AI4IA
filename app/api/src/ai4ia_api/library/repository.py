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

    async def delete_document(self, user_id: str, document_id: str) -> None: ...

    async def find_by_dedupe_key(
        self, user_id: str, content_hash: str, analyzer_id: str | None
    ) -> UserDocument | None: ...

    # --- analyzers (built-ins are merged in by the implementation) ---
    async def create_analyzer(self, analyzer: Analyzer) -> Analyzer: ...

    async def get_analyzer(self, user_id: str, analyzer_id: str) -> Analyzer: ...

    async def list_analyzers(self, user_id: str) -> list[Analyzer]: ...

    async def delete_analyzer(self, user_id: str, analyzer_id: str) -> None: ...
