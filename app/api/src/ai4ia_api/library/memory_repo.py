"""In-memory DocumentLibraryRepository for local dev and tests.

Enforces the same ownership + dedupe + built-in-merge rules as the Cosmos
implementation so behavior is identical across stores.
"""
from __future__ import annotations

import asyncio

from .hashing import dedupe_key
from .models import BUILTIN_ANALYZER_IDS, BUILTIN_ANALYZERS, Analyzer, UserDocument
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
            return document

    async def get_document(self, user_id: str, document_id: str) -> UserDocument:
        doc = self._docs.get(user_id, {}).get(document_id)
        if doc is None or doc.userId != user_id:
            raise DocumentNotFoundError(document_id)
        return doc

    async def list_documents(self, user_id: str) -> list[UserDocument]:
        docs = list(self._docs.get(user_id, {}).values())
        return sorted(docs, key=lambda d: d.createdAt, reverse=True)

    async def update_document(self, document: UserDocument) -> UserDocument:
        async with self._lock:
            bucket = self._docs.get(document.userId, {})
            if document.id not in bucket:
                raise DocumentNotFoundError(document.id)
            document.touch()
            bucket[document.id] = document
            return document

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
