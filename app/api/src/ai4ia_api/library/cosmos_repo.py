"""Cosmos DB (NoSQL) DocumentLibraryRepository using AAD (managed identity).

Containers (created by infra/modules/data.bicep):
- ``userDocuments``  PK ``/userId``
- ``analyzers``      PK ``/userId`` (custom analyzers only; built-ins are merged
  in from :data:`library.models.BUILTIN_ANALYZERS` and never stored)

Azure SDKs are imported lazily so the app and tests run without them installed.
Ownership is enforced by the ``/userId`` partition *and* re-checked on read.
"""
from __future__ import annotations

from typing import Any

from .hashing import dedupe_key
from .models import BUILTIN_ANALYZER_IDS, BUILTIN_ANALYZERS, Analyzer, UserDocument
from .repository import (
    AnalyzerConflictError,
    AnalyzerNotFoundError,
    DocumentNotFoundError,
)


class CosmosDocumentLibraryRepository:
    def __init__(self, endpoint: str, database: str) -> None:
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=self._credential)
        db = self._client.get_database_client(database)
        self._docs = db.get_container_client("userDocuments")
        self._analyzers = db.get_container_client("analyzers")

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()

    @staticmethod
    def _to_doc(model: UserDocument | Analyzer) -> dict[str, Any]:
        return model.model_dump(mode="json")

    # --- documents ---
    async def create_document(self, document: UserDocument) -> UserDocument:
        await self._docs.create_item(self._to_doc(document))
        return document

    async def get_document(self, user_id: str, document_id: str) -> UserDocument:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            item = await self._docs.read_item(item=document_id, partition_key=user_id)
        except CosmosResourceNotFoundError as exc:
            raise DocumentNotFoundError(document_id) from exc
        doc = UserDocument.model_validate(item)
        if doc.userId != user_id:
            raise DocumentNotFoundError(document_id)
        return doc

    async def list_documents(self, user_id: str) -> list[UserDocument]:
        query = "SELECT * FROM c WHERE c.userId = @uid ORDER BY c.createdAt DESC"
        params = [{"name": "@uid", "value": user_id}]
        return [
            UserDocument.model_validate(item)
            async for item in self._docs.query_items(query=query, parameters=params)
        ]

    async def update_document(self, document: UserDocument) -> UserDocument:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        # Parity with the in-memory repo: updating an id that does not exist (or
        # is owned by another user) must raise rather than silently create the
        # item, which ``upsert_item`` would otherwise do. The extra point-read is
        # negligible — status transitions are infrequent.
        try:
            existing = await self._docs.read_item(
                item=document.id, partition_key=document.userId
            )
        except CosmosResourceNotFoundError as exc:
            raise DocumentNotFoundError(document.id) from exc
        if existing.get("userId") != document.userId:
            raise DocumentNotFoundError(document.id)
        document.touch()
        await self._docs.upsert_item(self._to_doc(document))
        return document

    async def delete_document(self, user_id: str, document_id: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            await self._docs.delete_item(item=document_id, partition_key=user_id)
        except CosmosResourceNotFoundError:
            return  # idempotent

    async def find_by_dedupe_key(
        self, user_id: str, content_hash: str, analyzer_id: str | None
    ) -> UserDocument | None:
        query = "SELECT * FROM c WHERE c.userId = @uid AND c.contentHash = @hash"
        params = [
            {"name": "@uid", "value": user_id},
            {"name": "@hash", "value": content_hash},
        ]
        target = dedupe_key(content_hash, analyzer_id)
        async for item in self._docs.query_items(query=query, parameters=params):
            doc = UserDocument.model_validate(item)
            if dedupe_key(doc.contentHash, doc.analyzerId) == target:
                return doc
        return None

    # --- analyzers ---
    async def create_analyzer(self, analyzer: Analyzer) -> Analyzer:
        if analyzer.id in BUILTIN_ANALYZER_IDS:
            raise AnalyzerConflictError(analyzer.id)
        await self._analyzers.create_item(self._to_doc(analyzer))
        return analyzer

    async def get_analyzer(self, user_id: str, analyzer_id: str) -> Analyzer:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        for builtin in BUILTIN_ANALYZERS:
            if builtin.id == analyzer_id:
                return builtin
        try:
            item = await self._analyzers.read_item(
                item=analyzer_id, partition_key=user_id
            )
        except CosmosResourceNotFoundError as exc:
            raise AnalyzerNotFoundError(analyzer_id) from exc
        analyzer = Analyzer.model_validate(item)
        if analyzer.userId != user_id:
            raise AnalyzerNotFoundError(analyzer_id)
        return analyzer

    async def list_analyzers(self, user_id: str) -> list[Analyzer]:
        query = "SELECT * FROM c WHERE c.userId = @uid ORDER BY c.createdAt DESC"
        params = [{"name": "@uid", "value": user_id}]
        custom = [
            Analyzer.model_validate(item)
            async for item in self._analyzers.query_items(query=query, parameters=params)
        ]
        return [*BUILTIN_ANALYZERS, *custom]

    async def delete_analyzer(self, user_id: str, analyzer_id: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        if analyzer_id in BUILTIN_ANALYZER_IDS:
            raise AnalyzerNotFoundError(analyzer_id)
        try:
            await self._analyzers.delete_item(item=analyzer_id, partition_key=user_id)
        except CosmosResourceNotFoundError:
            return  # idempotent
