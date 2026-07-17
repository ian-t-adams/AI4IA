"""In-memory DocumentLibraryRepository for local dev and tests.

Enforces the same ownership + dedupe + built-in-merge rules as the Cosmos
implementation so behavior is identical across stores.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence

from .hashing import dedupe_key
from .models import (
    BUILTIN_ANALYZER_IDS,
    BUILTIN_ANALYZERS,
    Analyzer,
    DocumentStatus,
    UserDocument,
    Visibility,
)
from .repository import (
    AnalyzerConflictError,
    AnalyzerNotFoundError,
    DocumentNotFoundError,
)


class InMemoryDocumentLibraryRepository:
    def __init__(self) -> None:
        # user_id -> {document_id: UserDocument}
        self._docs: dict[str, dict[str, UserDocument]] = {}
        # user_id -> {analyzer_id: Analyzer}
        self._analyzers: dict[str, dict[str, Analyzer]] = {}
        self._lock = asyncio.Lock()

    # --- documents ---
    async def create_document(self, document: UserDocument) -> UserDocument:
        async with self._lock:
            self._docs.setdefault(document.userId, {})[document.id] = document
            document._etag = "1"
            return document.model_copy(deep=True)

    async def get_document(self, user_id: str, document_id: str) -> UserDocument:
        doc = self._docs.get(user_id, {}).get(document_id)
        if doc is None or doc.userId != user_id:
            raise DocumentNotFoundError(document_id)
        return doc.model_copy(deep=True)

    async def list_documents(self, user_id: str) -> list[UserDocument]:
        docs = list(self._docs.get(user_id, {}).values())
        indexed = enumerate(docs)
        return [
            doc
            for _, doc in sorted(
                indexed,
                key=lambda item: (item[1].createdAt, item[0]),
                reverse=True,
            )
        ]

    async def list_shared_with(self, email: str) -> list[UserDocument]:
        principal = (email or "").strip().lower()
        if not principal:
            return []
        async with self._lock:
            shared = [
                doc
                for bucket in self._docs.values()
                for doc in bucket.values()
                if doc.visibility == Visibility.shared and principal in doc.acl
            ]
        return [
            doc
            for _, doc in sorted(
                enumerate(shared),
                key=lambda item: (item[1].updatedAt, item[0]),
                reverse=True,
            )
        ]

    async def get_by_id(self, document_id: str) -> UserDocument | None:
        async with self._lock:
            for bucket in self._docs.values():
                doc = bucket.get(document_id)
                if doc is not None:
                    return doc
        return None

    async def list_by_status(
        self, statuses: Sequence[DocumentStatus]
    ) -> list[UserDocument]:
        wanted = set(statuses)
        async with self._lock:
            return [
                doc.model_copy(deep=True)
                for bucket in self._docs.values()
                for doc in bucket.values()
                if doc.status in wanted
            ]

    async def update_document(self, document: UserDocument) -> UserDocument:
        async with self._lock:
            bucket = self._docs.get(document.userId, {})
            if document.id not in bucket:
                raise DocumentNotFoundError(document.id)
            document.touch()
            current = bucket[document.id]
            document._etag = str(int(current._etag or "0") + 1)
            bucket[document.id] = document
            return document.model_copy(deep=True)

    async def patch_ingest_fields(
        self,
        document: UserDocument,
        changes: dict[str, object],
        *,
        require_status: DocumentStatus | None = None,
    ) -> UserDocument:
        async with self._lock:
            bucket = self._docs.get(document.userId, {})
            current = bucket.get(document.id)
            if current is None:
                raise DocumentNotFoundError(document.id)
            if require_status is not None and current.status != require_status:
                return current.model_copy(deep=True)
            for field_name, value in changes.items():
                setattr(current, field_name, value)
            current.touch()
            current._etag = str(int(current._etag or "0") + 1)
            return current.model_copy(deep=True)

    async def delete_document(self, user_id: str, document_id: str) -> None:
        async with self._lock:
            bucket = self._docs.get(user_id, {})
            # Idempotent delete; ownership is implicit in the per-user bucket.
            bucket.pop(document_id, None)

    async def find_by_dedupe_key(
        self, user_id: str, content_hash: str, analyzer_id: str | None
    ) -> UserDocument | None:
        key = dedupe_key(content_hash, analyzer_id)
        for doc in self._docs.get(user_id, {}).values():
            if dedupe_key(doc.contentHash, doc.analyzerId) == key:
                return doc
        return None

    # --- analyzers ---
    async def create_analyzer(self, analyzer: Analyzer) -> Analyzer:
        if analyzer.id in BUILTIN_ANALYZER_IDS:
            raise AnalyzerConflictError(analyzer.id)
        async with self._lock:
            self._analyzers.setdefault(analyzer.userId, {})[analyzer.id] = analyzer
            return analyzer

    async def get_analyzer(self, user_id: str, analyzer_id: str) -> Analyzer:
        for builtin in BUILTIN_ANALYZERS:
            if builtin.id == analyzer_id:
                return builtin
        analyzer = self._analyzers.get(user_id, {}).get(analyzer_id)
        if analyzer is None:
            raise AnalyzerNotFoundError(analyzer_id)
        return analyzer

    async def list_analyzers(self, user_id: str) -> list[Analyzer]:
        custom = sorted(
            self._analyzers.get(user_id, {}).values(),
            key=lambda a: a.createdAt,
            reverse=True,
        )
        return [*BUILTIN_ANALYZERS, *custom]

    async def delete_analyzer(self, user_id: str, analyzer_id: str) -> None:
        if analyzer_id in BUILTIN_ANALYZER_IDS:
            # Built-ins are not deletable; treat as not-found for the owner API.
            raise AnalyzerNotFoundError(analyzer_id)
        async with self._lock:
            self._analyzers.get(user_id, {}).pop(analyzer_id, None)
