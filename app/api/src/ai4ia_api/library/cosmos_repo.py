# pyright: reportArgumentType=false, reportCallIssue=false
# ^ Azure Cosmos SDK typing friction, not real defects: container.query_items's
#   `parameters` is typed list[dict[str, object]], but our list[dict[str, str]]
#   literals are rejected by list/dict invariance, which also makes the query_items
#   overloads fail to resolve. The queries are correct at runtime. Scoped to this
#   Cosmos repo module so the rules stay active everywhere else.
"""Cosmos DB (NoSQL) DocumentLibraryRepository using AAD (managed identity).

Containers (created by infra/modules/data.bicep):
- ``userDocuments``  PK ``/userId``
- ``analyzers``      PK ``/userId`` (custom analyzers only; built-ins are merged
  in from :data:`library.models.BUILTIN_ANALYZERS` and never stored)

Azure SDKs are imported lazily so the app and tests run without them installed.
Ownership is enforced by the ``/userId`` partition *and* re-checked on read.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel

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
    DocumentConflictError,
    DocumentNotFoundError,
)

MAX_COSMOS_PATCH_OPERATIONS = 10


def _patch_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


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

    @staticmethod
    def _from_document(item: dict[str, Any]) -> UserDocument:
        document = UserDocument.model_validate(item)
        document._etag = item.get("_etag")
        return document

    # --- documents ---
    async def create_document(self, document: UserDocument) -> UserDocument:
        created = await self._docs.create_item(self._to_doc(document))
        document._etag = created.get("_etag")
        return document

    async def get_document(self, user_id: str, document_id: str) -> UserDocument:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            item = await self._docs.read_item(item=document_id, partition_key=user_id)
        except CosmosResourceNotFoundError as exc:
            raise DocumentNotFoundError(document_id) from exc
        doc = self._from_document(item)
        if doc.userId != user_id:
            raise DocumentNotFoundError(document_id)
        return doc

    async def list_documents(self, user_id: str) -> list[UserDocument]:
        query = "SELECT * FROM c WHERE c.userId = @uid ORDER BY c.createdAt DESC"
        params = [{"name": "@uid", "value": user_id}]
        return [
            self._from_document(item)
            async for item in self._docs.query_items(query=query, parameters=params)
        ]

    async def list_shared_with(self, email: str) -> list[UserDocument]:
        """Cross-partition: the ``shared`` documents whose ``acl`` contains
        ``email`` (the grantee's normalized address). Used by the "shared with me"
        listing and to widen retrieval to shared documents; not on the per-turn
        hot path for users without inbound shares (the query simply returns []).
        """
        principal = (email or "").strip().lower()
        if not principal:
            return []
        query = (
            "SELECT * FROM c WHERE c.visibility = @shared "
            "AND ARRAY_CONTAINS(c.acl, @email) ORDER BY c.updatedAt DESC"
        )
        params = [
            {"name": "@shared", "value": Visibility.shared.value},
            {"name": "@email", "value": principal},
        ]
        return [
            self._from_document(item)
            async for item in self._docs.query_items(query=query, parameters=params)
        ]

    async def get_by_id(self, document_id: str) -> UserDocument | None:
        """Cross-partition fetch of a single document by id, regardless of owner.

        Returns ``None`` when no such document exists. The caller MUST gate the
        result with :func:`access.can_access` before exposing it — this method
        performs no ownership/sharing check itself.
        """
        document_id = (document_id or "").strip()
        if not document_id:
            return None
        query = "SELECT * FROM c WHERE c.id = @id"
        params = [{"name": "@id", "value": document_id}]
        async for item in self._docs.query_items(query=query, parameters=params):
            return self._from_document(item)
        return None

    async def list_by_status(
        self, statuses: Sequence[DocumentStatus]
    ) -> list[UserDocument]:
        """Cross-partition scan for the startup recovery sweep (all owners).

        Used only at startup to fail out documents left mid-ingest by an
        interrupted worker; not on any hot path.
        """
        values = [
            s.value if isinstance(s, DocumentStatus) else str(s) for s in statuses
        ]
        if not values:
            return []
        query = "SELECT * FROM c WHERE ARRAY_CONTAINS(@statuses, c.status)"
        params = [{"name": "@statuses", "value": values}]
        return [
            self._from_document(item)
            async for item in self._docs.query_items(query=query, parameters=params)
        ]

    async def update_document(self, document: UserDocument) -> UserDocument:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceNotFoundError,
        )

        # Parity with the in-memory repo: updating an id that does not exist (or
        # is owned by another user) must raise rather than silently create the
        # item, which ``upsert_item`` would otherwise do. We read first to
        # enforce existence + ownership (the extra point-read is negligible —
        # status transitions are infrequent). The write precondition is the
        # *caller's own* etag, captured when they loaded the document via
        # ``get_document``/``create_document`` — matching the pattern in
        # ``patch_ingest_fields`` below. Using a freshly re-read etag here
        # instead (as this used to) would always happen to match, silently
        # discarding any edit that landed between the caller's load and this
        # call — e.g. two concurrent annotation adds on the same document,
        # or the enrich worker patching status while a user edits shares.
        try:
            existing = await self._docs.read_item(
                item=document.id, partition_key=document.userId
            )
        except CosmosResourceNotFoundError as exc:
            raise DocumentNotFoundError(document.id) from exc
        if existing.get("userId") != document.userId:
            raise DocumentNotFoundError(document.id)
        document.touch()
        # Fall back to the just-read etag only for a document that was never
        # actually loaded (so has no etag of its own to defend).
        write_etag = document._etag or existing.get("_etag")
        try:
            updated = await self._docs.replace_item(
                item=document.id,
                body=self._to_doc(document),
                etag=write_etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosResourceNotFoundError as exc:
            # Deleted between the read and the write.
            raise DocumentNotFoundError(document.id) from exc
        except CosmosAccessConditionFailedError as exc:
            # Etag moved (concurrent modify since the caller's load). The
            # document still exists — this is a genuine conflict, not a
            # not-found, so it must not be reported as one: a 404 here would
            # be a false negative that could lead a caller to (re)create a
            # "missing" document instead of reloading the real, current one.
            raise DocumentConflictError(document.id) from exc
        document._etag = updated.get("_etag")
        return document

    async def patch_ingest_fields(
        self,
        document: UserDocument,
        changes: dict[str, object],
        *,
        require_status: DocumentStatus | None = None,
    ) -> UserDocument:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceNotFoundError,
        )

        etag = document._etag
        for _attempt in range(3):
            if len(changes) + 1 > MAX_COSMOS_PATCH_OPERATIONS:
                raise ValueError(
                    "ingest field patch exceeds the Cosmos 10-operation limit"
                )
            operations = [
                {
                    "op": "set",
                    "path": f"/{field_name}",
                    "value": _patch_value(value),
                }
                for field_name, value in changes.items()
            ]
            operations.append(
                {
                    "op": "set",
                    "path": "/updatedAt",
                    "value": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
            try:
                updated = await self._docs.patch_item(
                    item=document.id,
                    partition_key=document.userId,
                    patch_operations=operations,
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                )
                return self._from_document(updated)
            except CosmosResourceNotFoundError as exc:
                raise DocumentNotFoundError(document.id) from exc
            except CosmosAccessConditionFailedError:
                try:
                    latest = await self._docs.read_item(
                        item=document.id, partition_key=document.userId
                    )
                except CosmosResourceNotFoundError as exc:
                    raise DocumentNotFoundError(document.id) from exc
                latest_document = self._from_document(latest)
                if (
                    require_status is not None
                    and latest_document.status != require_status
                ):
                    return latest_document
                etag = latest.get("_etag")
        raise RuntimeError("document ingest manifest update conflicted repeatedly")

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
