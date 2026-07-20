# pyright: reportArgumentType=false, reportCallIssue=false
"""Canonical, user-partitioned semantic memory storage in Cosmos DB for NoSQL."""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from .models import MemoryRecord

_STATE_ID = "state"
_MEMORY_TYPE = "memory"
_STATE_TYPE = "state"
_OPERATION_TYPE = "operation"
_DOCUMENT_STATE_TYPE = "documentState"
_PURGE_BATCH_SIZE = 100
_MAX_PURGE_PAGES = 1_000
_RECEIPT_TTL_SECONDS = 7 * 24 * 60 * 60

ForgetScope = Literal["user", "session", "document"]


class MemoryNotFoundError(Exception):
    """The memory does not exist in the caller's logical partition."""


class MemoryConflictError(Exception):
    """A conditional memory mutation lost a concurrency race."""


class MemoryFenceConflict(MemoryConflictError):
    """The per-user write fence changed before a mutation committed."""


@dataclass(frozen=True)
class ForgetCutoff:
    key: str
    scope: ForgetScope
    scope_id: str | None
    before_epoch: int
    started_at: datetime

    def applies(self, record: MemoryRecord) -> bool:
        if record.write_epoch >= self.before_epoch:
            return False
        if self.scope == "user":
            return True
        if self.scope == "session":
            return record.session_id == self.scope_id
        return record.document_id == self.scope_id


@dataclass(frozen=True)
class MemoryState:
    user_id: str
    epoch: int
    etag: str
    cutoffs: tuple[ForgetCutoff, ...]
    updated_at: datetime


def operation_ids(user_id: str, idempotency_key: str) -> tuple[str, str]:
    """Return deterministic, opaque memory and receipt ids for a create request."""
    digest = hashlib.sha256(
        f"{user_id}\0create\0{idempotency_key}".encode("utf-8")
    ).hexdigest()
    return f"m-{digest[:32]}", f"op-{digest}"


def operation_id(
    user_id: str,
    operation: Literal["update", "delete"],
    memory_id: str,
    idempotency_key: str,
) -> str:
    """Return an opaque receipt id scoped to one explicit item mutation."""
    digest = hashlib.sha256(
        f"{user_id}\0{operation}\0{memory_id}\0{idempotency_key}".encode("utf-8")
    ).hexdigest()
    return f"op-{digest}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"memory document is missing {field}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cutoff_key(scope: ForgetScope, scope_id: str | None) -> str:
    if scope == "user":
        return "user"
    if not scope_id:
        raise ValueError(f"{scope} forget requires an id")
    digest = hashlib.sha256(scope_id.encode("utf-8")).hexdigest()[:24]
    return f"{scope}:{digest}"


def _document_state_id(document_id: str) -> str:
    digest = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
    return f"document-{digest}"


def _batch_failure(exc: BaseException) -> tuple[int | None, int | None]:
    index = getattr(exc, "error_index", None)
    status = getattr(exc, "status_code", None)
    responses = getattr(exc, "operation_responses", None)
    if isinstance(index, int) and isinstance(responses, list) and index < len(responses):
        response = responses[index]
        if isinstance(response, dict):
            raw = response.get("statusCode") or response.get("status_code")
            if raw is not None:
                status = raw
    try:
        parsed_status = int(status) if status is not None else None
    except (TypeError, ValueError):
        parsed_status = None
    return parsed_status, index if isinstance(index, int) else None


class CosmosMemoryStore:
    """Cosmos repository with ETag writes and per-user epoch fencing."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        database: str | None = None,
        expected_dim: int = 3_072,
        embedding_model: str | None = None,
        container: Any | None = None,
    ) -> None:
        self._expected_dim = expected_dim
        self._embedding_model = embedding_model
        self._client: Any | None = None
        self._credential: Any | None = None
        if container is not None:
            self._container = container
            return
        if not endpoint or not database:
            raise ValueError("Cosmos memory requires an endpoint and database")
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=self._credential)
        db = self._client.get_database_client(database)
        self._container = db.get_container_client("memories")

    async def ensure_ready(self) -> None:
        await self._container.read()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._credential is not None:
            await self._credential.close()
            self._credential = None

    async def capture_state(self, user_id: str) -> MemoryState:
        from azure.cosmos.exceptions import (
            CosmosResourceExistsError,
            CosmosResourceNotFoundError,
        )

        try:
            document = await self._container.read_item(
                item=_STATE_ID, partition_key=user_id
            )
        except CosmosResourceNotFoundError:
            now = _now()
            body = {
                "id": _STATE_ID,
                "type": _STATE_TYPE,
                "userId": user_id,
                "epoch": 0,
                "cutoffs": [],
                "updatedAt": _iso(now),
            }
            try:
                document = await self._container.create_item(body)
            except CosmosResourceExistsError:
                document = await self._container.read_item(
                    item=_STATE_ID, partition_key=user_id
                )
        return self._state_from_document(user_id, document)

    async def get_operation(
        self,
        user_id: str,
        operation_id: str,
        *,
        operation: Literal["create", "update", "delete"] | None = None,
        memory_id: str | None = None,
    ) -> str | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            item = await self._container.read_item(
                item=operation_id, partition_key=user_id
            )
        except CosmosResourceNotFoundError:
            return None
        if item.get("type") != _OPERATION_TYPE or item.get("userId") != user_id:
            return None
        if operation is not None and item.get("operation") != operation:
            raise MemoryConflictError("idempotency key was reused for another operation")
        stored_id = item.get("memoryId")
        if memory_id is not None and stored_id != memory_id:
            raise MemoryConflictError("idempotency key was reused for another memory")
        return str(stored_id) if stored_id else None

    async def get_memory(
        self,
        user_id: str,
        memory_id: str,
        *,
        state: MemoryState | None = None,
    ) -> MemoryRecord:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        active_state = state or await self.capture_state(user_id)
        try:
            document = await self._container.read_item(
                item=memory_id, partition_key=user_id
            )
        except CosmosResourceNotFoundError as exc:
            raise MemoryNotFoundError(memory_id) from exc
        record = self._record_from_document(user_id, document)
        if any(cutoff.applies(record) for cutoff in active_state.cutoffs):
            raise MemoryNotFoundError(memory_id)
        return record

    async def list_memories(
        self, user_id: str, *, limit: int = 100
    ) -> list[MemoryRecord]:
        state = await self.capture_state(user_id)
        bounded = max(1, min(limit, 200))
        fetch_limit = bounded if not state.cutoffs else min(bounded * 4, 800)
        query = (
            "SELECT TOP @limit * FROM c "
            "WHERE c.userId = @uid AND c.type = 'memory' "
            "ORDER BY c.updatedAt DESC"
        )
        documents = await self._query(
            query,
            [
                {"name": "@limit", "value": fetch_limit},
                {"name": "@uid", "value": user_id},
            ],
            user_id,
        )
        records = [
            self._record_from_document(user_id, document) for document in documents
        ]
        return [
            record
            for record in records
            if not any(cutoff.applies(record) for cutoff in state.cutoffs)
        ][:bounded]

    async def search(
        self,
        user_id: str,
        query_vector: Sequence[float],
        top_k: int,
        *,
        state: MemoryState | None = None,
    ) -> list[MemoryRecord]:
        vector = list(query_vector)
        self._validate_vector(vector)
        active_state = state or await self.capture_state(user_id)
        bounded = max(1, min(top_k, 100))
        fetch_limit = bounded if not active_state.cutoffs else min(max(32, bounded * 8), 200)
        query = (
            "SELECT TOP @limit c.id, c.type, c.userId, c.text, c.sessionId, "
            "c.documentId, c.kind, c.origin, c.locked, c.embeddingModel, "
            "c.writeEpoch, c.version, c.createdAt, c.updatedAt, c._etag, "
            "VectorDistance(c.embedding, @embedding) AS similarity "
            "FROM c WHERE c.userId = @uid AND c.type = 'memory' "
            "ORDER BY VectorDistance(c.embedding, @embedding)"
        )
        documents = await self._query(
            query,
            [
                {"name": "@limit", "value": fetch_limit},
                {"name": "@uid", "value": user_id},
                {"name": "@embedding", "value": vector},
            ],
            user_id,
        )
        records: list[MemoryRecord] = []
        for document in documents:
            similarity = document.pop("similarity", None)
            score = None
            if isinstance(similarity, int | float):
                # For a cosine policy Cosmos projects similarity (higher is better)
                # even though the system function is named VectorDistance.
                score = max(-1.0, min(1.0, float(similarity)))
            record = self._record_from_document(user_id, document, score=score)
            if any(cutoff.applies(record) for cutoff in active_state.cutoffs):
                continue
            records.append(record)
            if len(records) >= bounded:
                break
        return records

    async def commit_create(
        self,
        state: MemoryState,
        record: MemoryRecord,
        vector: Sequence[float],
        *,
        operation_id: str | None = None,
    ) -> MemoryRecord:
        from azure.cosmos.exceptions import CosmosBatchOperationError

        now = _now()
        document = self._memory_document(
            record,
            vector,
            write_epoch=state.epoch,
            version=max(1, record.version),
            created_at=record.created_at,
            updated_at=now,
        )
        operations: list[tuple[Any, ...]] = [
            (
                "replace",
                (_STATE_ID, self._state_document(state, updated_at=now)),
                {"if_match_etag": state.etag},
            ),
            ("create", (document,), {}),
        ]
        if operation_id is not None:
            operations.append(
                ("create", (self._operation_document(
                    operation_id,
                    user_id=state.user_id,
                    operation="create",
                    memory_id=record.id,
                    created_at=now,
                ),), {})
            )
        try:
            await self._container.execute_item_batch(
                batch_operations=operations, partition_key=state.user_id
            )
        except CosmosBatchOperationError as exc:
            status, index = _batch_failure(exc)
            if status == 412 and index == 0:
                raise MemoryFenceConflict(record.id) from exc
            if status == 409 and operation_id is not None:
                existing_id = await self.get_operation(
                    state.user_id,
                    operation_id,
                    operation="create",
                    memory_id=record.id,
                )
                if existing_id == record.id:
                    return await self.get_memory(state.user_id, record.id)
            if status == 409:
                raise MemoryConflictError(record.id) from exc
            raise
        return await self.get_memory(state.user_id, record.id)

    async def commit_update(
        self,
        state: MemoryState,
        record: MemoryRecord,
        vector: Sequence[float],
        *,
        expected_etag: str,
        operation_id: str | None = None,
    ) -> MemoryRecord:
        from azure.cosmos.exceptions import CosmosBatchOperationError

        now = _now()
        document = self._memory_document(
            record,
            vector,
            write_epoch=state.epoch,
            version=record.version,
            created_at=record.created_at,
            updated_at=now,
        )
        operations: list[tuple[Any, ...]] = [
            (
                "replace",
                (_STATE_ID, self._state_document(state, updated_at=now)),
                {"if_match_etag": state.etag},
            ),
            (
                "replace",
                (record.id, document),
                {"if_match_etag": expected_etag},
            ),
        ]
        if operation_id is not None:
            operations.append(
                ("create", (self._operation_document(
                    operation_id,
                    user_id=state.user_id,
                    operation="update",
                    memory_id=record.id,
                    created_at=now,
                ),), {})
            )
        try:
            await self._container.execute_item_batch(
                batch_operations=operations, partition_key=state.user_id
            )
        except CosmosBatchOperationError as exc:
            status, index = _batch_failure(exc)
            if status == 412 and index == 0:
                raise MemoryFenceConflict(record.id) from exc
            if status == 404:
                raise MemoryNotFoundError(record.id) from exc
            if status == 412:
                raise MemoryConflictError(record.id) from exc
            if status == 409 and operation_id is not None:
                existing_id = await self.get_operation(
                    state.user_id,
                    operation_id,
                    operation="update",
                    memory_id=record.id,
                )
                if existing_id == record.id:
                    return await self.get_memory(state.user_id, record.id)
            raise
        return await self.get_memory(state.user_id, record.id)

    async def commit_delete(
        self,
        state: MemoryState,
        memory_id: str,
        *,
        expected_etag: str,
        operation_id: str | None = None,
    ) -> bool:
        from azure.cosmos.exceptions import (
            CosmosBatchOperationError,
            CosmosResourceNotFoundError,
        )

        operations: list[tuple[Any, ...]] = [
            (
                "replace",
                (_STATE_ID, self._state_document(state, updated_at=_now())),
                {"if_match_etag": state.etag},
            ),
            ("delete", (memory_id,), {"if_match_etag": expected_etag}),
        ]
        if operation_id is not None:
            operations.append(
                ("create", (self._operation_document(
                    operation_id,
                    user_id=state.user_id,
                    operation="delete",
                    memory_id=memory_id,
                    created_at=_now(),
                ),), {})
            )
        try:
            await self._container.execute_item_batch(
                batch_operations=operations, partition_key=state.user_id
            )
        except CosmosBatchOperationError as exc:
            status, index = _batch_failure(exc)
            if status == 412 and index == 0:
                raise MemoryFenceConflict(memory_id) from exc
            if status == 404:
                raise MemoryNotFoundError(memory_id) from exc
            if status == 412:
                raise MemoryConflictError(memory_id) from exc
            if status == 409 and operation_id is not None:
                existing_id = await self.get_operation(
                    state.user_id,
                    operation_id,
                    operation="delete",
                    memory_id=memory_id,
                )
                if existing_id == memory_id:
                    return True
            raise
        try:
            await self._container.read_item(item=memory_id, partition_key=state.user_id)
        except CosmosResourceNotFoundError:
            return True
        raise RuntimeError("memory deletion could not be verified")

    async def forget(
        self,
        user_id: str,
        scope: ForgetScope,
        scope_id: str | None = None,
    ) -> int:
        from azure.cosmos.exceptions import CosmosBatchOperationError

        cutoff = await self._begin_forget(user_id, scope, scope_id)
        deleted = 0
        conflicts = 0
        for _page in range(_MAX_PURGE_PAGES):
            documents = await self._list_purge_candidates(user_id, cutoff)
            if not documents:
                break
            operations = [
                (
                    "delete",
                    (str(document["id"]),),
                    {"if_match_etag": str(document["_etag"])},
                )
                for document in documents
            ]
            try:
                await self._container.execute_item_batch(
                    batch_operations=operations, partition_key=user_id
                )
                deleted += len(operations)
                conflicts = 0
            except CosmosBatchOperationError as exc:
                status, _index = _batch_failure(exc)
                if status not in {404, 412}:
                    raise
                conflicts += 1
                if conflicts >= 20:
                    raise MemoryConflictError("forget purge did not converge") from exc
        else:
            raise RuntimeError("memory forget exceeded the bounded purge limit")

        if await self._count_purge_candidates(user_id, cutoff) != 0:
            raise RuntimeError("memory forget verification failed")
        await self._clear_cutoff(user_id, cutoff)
        return deleted

    async def erase_user(self, user_id: str) -> int:
        return await self.forget(user_id, "user")

    async def erase_session(self, user_id: str, session_id: str) -> int:
        return await self.forget(user_id, "session", session_id)

    async def erase_document(self, user_id: str, document_id: str) -> int:
        return await self.forget(user_id, "document", document_id)

    async def replace_document(
        self,
        user_id: str,
        document_id: str,
        records: Sequence[MemoryRecord],
        vectors: Sequence[Sequence[float]],
    ) -> int:
        """Atomically replace one document's memories unless its source was deleted."""
        from azure.cosmos.exceptions import CosmosBatchOperationError

        if len(records) != len(vectors):
            raise ValueError("document memory records and vectors must have equal length")
        marker_id = _document_state_id(document_id)
        for _attempt in range(8):
            state = await self.capture_state(user_id)
            marker = await self._read_document_state(user_id, marker_id)
            if marker is not None and marker.get("status") == "deleted":
                raise MemoryNotFoundError(document_id)
            existing = await self._query(
                "SELECT TOP 101 c.id, c._etag FROM c "
                "WHERE c.userId = @uid AND c.type = 'memory' "
                "AND c.documentId = @documentId",
                [
                    {"name": "@uid", "value": user_id},
                    {"name": "@documentId", "value": document_id},
                ],
                user_id,
            )
            if 2 + len(existing) + len(records) > 100:
                raise ValueError(
                    "document has too many saved memories for an atomic replacement"
                )
            now = _now()
            marker_body = self._document_state_document(
                marker_id,
                user_id=user_id,
                document_id=document_id,
                status="active",
                updated_at=now,
            )
            operations: list[tuple[Any, ...]] = [
                (
                    "replace",
                    (_STATE_ID, self._state_document(state, updated_at=now)),
                    {"if_match_etag": state.etag},
                )
            ]
            if marker is None:
                operations.append(("create", (marker_body,), {}))
            else:
                operations.append(
                    (
                        "replace",
                        (marker_id, marker_body),
                        {"if_match_etag": str(marker["_etag"])},
                    )
                )
            operations.extend(
                (
                    "delete",
                    (str(document["id"]),),
                    {"if_match_etag": str(document["_etag"])},
                )
                for document in existing
            )
            for record, vector in zip(records, vectors):
                if record.user_id != user_id or record.document_id != document_id:
                    raise ValueError("document memory ownership or scope is invalid")
                operations.append(
                    (
                        "create",
                        (
                            self._memory_document(
                                record,
                                vector,
                                write_epoch=state.epoch,
                                version=max(1, record.version),
                                created_at=record.created_at,
                                updated_at=now,
                            ),
                        ),
                        {},
                    )
                )
            try:
                await self._container.execute_item_batch(
                    batch_operations=operations, partition_key=user_id
                )
                return len(records)
            except CosmosBatchOperationError as exc:
                status, _index = _batch_failure(exc)
                if status in {404, 409, 412}:
                    continue
                raise
        raise MemoryConflictError("document memory replacement did not converge")

    async def delete_document_source(self, user_id: str, document_id: str) -> int:
        """Permanently fence document-memory saves, then purge its existing records."""
        from azure.cosmos.exceptions import CosmosBatchOperationError

        marker_id = _document_state_id(document_id)
        for _attempt in range(8):
            state = await self.capture_state(user_id)
            marker = await self._read_document_state(user_id, marker_id)
            if marker is not None and marker.get("status") == "deleted":
                break
            now = _now()
            marker_body = self._document_state_document(
                marker_id,
                user_id=user_id,
                document_id=document_id,
                status="deleted",
                updated_at=now,
            )
            operations: list[tuple[Any, ...]] = [
                (
                    "replace",
                    (_STATE_ID, self._state_document(state, updated_at=now)),
                    {"if_match_etag": state.etag},
                )
            ]
            if marker is None:
                operations.append(("create", (marker_body,), {}))
            else:
                operations.append(
                    (
                        "replace",
                        (marker_id, marker_body),
                        {"if_match_etag": str(marker["_etag"])},
                    )
                )
            try:
                await self._container.execute_item_batch(
                    batch_operations=operations, partition_key=user_id
                )
                break
            except CosmosBatchOperationError as exc:
                status, _index = _batch_failure(exc)
                if status in {404, 409, 412}:
                    continue
                raise
        else:
            raise MemoryConflictError("document deletion fence did not converge")
        return await self.forget(user_id, "document", document_id)

    async def _read_document_state(
        self, user_id: str, marker_id: str
    ) -> dict[str, Any] | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            document = await self._container.read_item(
                item=marker_id, partition_key=user_id
            )
        except CosmosResourceNotFoundError:
            return None
        if (
            document.get("type") != _DOCUMENT_STATE_TYPE
            or document.get("userId") != user_id
        ):
            raise MemoryNotFoundError(marker_id)
        return document

    async def _begin_forget(
        self, user_id: str, scope: ForgetScope, scope_id: str | None
    ) -> ForgetCutoff:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

        key = _cutoff_key(scope, scope_id)
        for _attempt in range(8):
            state = await self.capture_state(user_id)
            for cutoff in state.cutoffs:
                if cutoff.key == key:
                    return cutoff
            now = _now()
            cutoff = ForgetCutoff(
                key=key,
                scope=scope,
                scope_id=scope_id,
                before_epoch=state.epoch + 1,
                started_at=now,
            )
            body = self._state_document(
                state,
                epoch=cutoff.before_epoch,
                cutoffs=(*state.cutoffs, cutoff),
                updated_at=now,
            )
            try:
                await self._container.replace_item(
                    item=_STATE_ID,
                    body=body,
                    etag=state.etag,
                    match_condition=MatchConditions.IfNotModified,
                )
                return cutoff
            except CosmosAccessConditionFailedError:
                continue
        raise MemoryConflictError("could not establish memory forget fence")

    async def _clear_cutoff(self, user_id: str, cutoff: ForgetCutoff) -> None:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

        for _attempt in range(8):
            state = await self.capture_state(user_id)
            retained = tuple(
                item
                for item in state.cutoffs
                if item.key != cutoff.key
                and not (
                    cutoff.scope == "user"
                    and item.before_epoch <= cutoff.before_epoch
                )
            )
            if retained == state.cutoffs:
                return
            body = self._state_document(
                state, cutoffs=retained, updated_at=_now()
            )
            try:
                await self._container.replace_item(
                    item=_STATE_ID,
                    body=body,
                    etag=state.etag,
                    match_condition=MatchConditions.IfNotModified,
                )
                return
            except CosmosAccessConditionFailedError:
                continue
        raise MemoryConflictError("could not clear memory forget fence")

    async def _list_purge_candidates(
        self, user_id: str, cutoff: ForgetCutoff
    ) -> list[dict[str, Any]]:
        query, parameters = self._purge_query(
            user_id, cutoff, select="SELECT TOP @limit c.id, c._etag"
        )
        parameters.append({"name": "@limit", "value": _PURGE_BATCH_SIZE})
        return await self._query(query, parameters, user_id)

    async def _count_purge_candidates(
        self, user_id: str, cutoff: ForgetCutoff
    ) -> int:
        query, parameters = self._purge_query(
            user_id, cutoff, select="SELECT VALUE COUNT(1)"
        )
        results = await self._query(query, parameters, user_id)
        return int(results[0]) if results else 0

    def _purge_query(
        self,
        user_id: str,
        cutoff: ForgetCutoff,
        *,
        select: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        clauses = [
            "c.userId = @uid",
            "c.type = 'memory'",
            "(NOT IS_DEFINED(c.writeEpoch) OR c.writeEpoch < @epoch)",
        ]
        parameters: list[dict[str, Any]] = [
            {"name": "@uid", "value": user_id},
            {"name": "@epoch", "value": cutoff.before_epoch},
        ]
        if cutoff.scope == "session":
            clauses.append("c.sessionId = @scopeId")
            parameters.append({"name": "@scopeId", "value": cutoff.scope_id})
        elif cutoff.scope == "document":
            clauses.append("c.documentId = @scopeId")
            parameters.append({"name": "@scopeId", "value": cutoff.scope_id})
        return f"{select} FROM c WHERE {' AND '.join(clauses)}", parameters

    async def _query(
        self,
        query: str,
        parameters: list[dict[str, Any]],
        partition_key: str,
    ) -> list[Any]:
        return [
            item
            async for item in self._container.query_items(
                query=query,
                parameters=parameters,
                partition_key=partition_key,
            )
        ]

    def _validate_vector(self, vector: Sequence[float]) -> None:
        if len(vector) != self._expected_dim:
            raise ValueError(
                f"embedding dimension {len(vector)} != expected {self._expected_dim}"
            )

    def _memory_document(
        self,
        record: MemoryRecord,
        vector: Sequence[float],
        *,
        write_epoch: int,
        version: int,
        created_at: datetime,
        updated_at: datetime,
    ) -> dict[str, Any]:
        embedding = [float(value) for value in vector]
        self._validate_vector(embedding)
        return {
            "id": record.id,
            "type": _MEMORY_TYPE,
            "userId": record.user_id,
            "text": record.text,
            "sessionId": record.session_id,
            "documentId": record.document_id,
            "kind": record.kind,
            "origin": record.origin,
            "locked": record.locked,
            "embedding": embedding,
            "embeddingModel": record.embedding_model or self._embedding_model,
            "writeEpoch": write_epoch,
            "version": version,
            "createdAt": _iso(created_at),
            "updatedAt": _iso(updated_at),
        }

    @staticmethod
    def _operation_document(
        operation_id: str,
        *,
        user_id: str,
        operation: Literal["create", "update", "delete"],
        memory_id: str,
        created_at: datetime,
    ) -> dict[str, Any]:
        return {
            "id": operation_id,
            "type": _OPERATION_TYPE,
            "userId": user_id,
            "operation": operation,
            "memoryId": memory_id,
            "createdAt": _iso(created_at),
            "ttl": _RECEIPT_TTL_SECONDS,
        }

    def _record_from_document(
        self,
        user_id: str,
        document: dict[str, Any],
        *,
        score: float | None = None,
    ) -> MemoryRecord:
        if (
            document.get("type") != _MEMORY_TYPE
            or document.get("userId") != user_id
        ):
            raise MemoryNotFoundError(str(document.get("id") or ""))
        text = document.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("memory document has invalid text")
        return MemoryRecord(
            id=str(document["id"]),
            user_id=user_id,
            text=text,
            session_id=document.get("sessionId"),
            document_id=document.get("documentId"),
            kind=str(document.get("kind") or "fact"),
            created_at=_parse_datetime(document.get("createdAt"), field="createdAt"),
            updated_at=_parse_datetime(document.get("updatedAt"), field="updatedAt"),
            version=int(document.get("version") or 1),
            etag=document.get("_etag"),
            origin=str(document.get("origin") or "implicit"),
            locked=bool(document.get("locked", False)),
            write_epoch=int(document.get("writeEpoch") or 0),
            embedding_model=document.get("embeddingModel"),
            score=score,
        )

    def _state_from_document(
        self, user_id: str, document: dict[str, Any]
    ) -> MemoryState:
        if document.get("type") != _STATE_TYPE or document.get("userId") != user_id:
            raise RuntimeError("invalid memory state document")
        etag = document.get("_etag")
        if not isinstance(etag, str) or not etag:
            raise RuntimeError("memory state document has no ETag")
        cutoffs: list[ForgetCutoff] = []
        raw_cutoffs = document.get("cutoffs", [])
        if not isinstance(raw_cutoffs, list):
            raise RuntimeError("memory state cutoffs are invalid")
        for raw in raw_cutoffs:
            if not isinstance(raw, dict):
                raise RuntimeError("memory state cutoff is invalid")
            scope = raw.get("scope")
            if scope not in {"user", "session", "document"}:
                raise RuntimeError("memory state cutoff scope is invalid")
            cutoffs.append(
                ForgetCutoff(
                    key=str(raw["key"]),
                    scope=scope,
                    scope_id=raw.get("scopeId"),
                    before_epoch=int(raw["beforeEpoch"]),
                    started_at=_parse_datetime(
                        raw.get("startedAt"), field="cutoff.startedAt"
                    ),
                )
            )
        return MemoryState(
            user_id=user_id,
            epoch=int(document.get("epoch") or 0),
            etag=etag,
            cutoffs=tuple(cutoffs),
            updated_at=_parse_datetime(document.get("updatedAt"), field="updatedAt"),
        )

    def _state_document(
        self,
        state: MemoryState,
        *,
        epoch: int | None = None,
        cutoffs: Sequence[ForgetCutoff] | None = None,
        updated_at: datetime,
    ) -> dict[str, Any]:
        active_cutoffs = state.cutoffs if cutoffs is None else tuple(cutoffs)
        return {
            "id": _STATE_ID,
            "type": _STATE_TYPE,
            "userId": state.user_id,
            "epoch": state.epoch if epoch is None else epoch,
            "cutoffs": [
                {
                    "key": cutoff.key,
                    "scope": cutoff.scope,
                    "scopeId": cutoff.scope_id,
                    "beforeEpoch": cutoff.before_epoch,
                    "startedAt": _iso(cutoff.started_at),
                }
                for cutoff in active_cutoffs
            ],
            "updatedAt": _iso(updated_at),
        }

    @staticmethod
    def _document_state_document(
        marker_id: str,
        *,
        user_id: str,
        document_id: str,
        status: Literal["active", "deleted"],
        updated_at: datetime,
    ) -> dict[str, Any]:
        return {
            "id": marker_id,
            "type": _DOCUMENT_STATE_TYPE,
            "userId": user_id,
            "documentId": document_id,
            "status": status,
            "updatedAt": _iso(updated_at),
        }
