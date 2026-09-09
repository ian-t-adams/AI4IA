"""Cosmos primitives for the opt-in, single-write-region deletion protocol.

The parent is an access barrier, not a cross-container transaction. Child writes
CAS a partition-local fence in the SAME transaction as their mutation. Retaining
closed fences is essential: neither a missing fence nor an elapsed lease means
that a delayed writer is safe.
"""
from __future__ import annotations

from datetime import timedelta
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from .deletion_models import (
    CAS_ATTEMPTS,
    CLEANUP_ITEMS,
    CONTROL_PARTITION,
    FENCE_ID,
    LEASE_SECONDS,
    STATUS_ITEMS,
    UPLOAD_ID_PREFIX,
    DeletionDisabledError,
    DeletionIntegrityError,
    DeletionLease,
    DeletionMigrationRequiredError,
    DeletionPage,
    DeletionRecord,
    DeletionStatus,
    DeletionUnavailableError,
    InitializationPage,
    InitializationRecord,
    RolloutApproval,
    SessionInitialization,
    UploadIntent,
    initialization_after,
    initialization_cursor,
    now_utc,
)
from .models import Session
from .repository import SessionNotFoundError

BatchOperation = tuple[str, tuple[Any, ...], dict[str, Any]]


def body_only(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if not key.startswith("_")}


def require_etag(raw: Any) -> str:
    if not isinstance(raw, Mapping):
        raise DeletionUnavailableError("Missing concurrency response")
    etag = raw.get("_etag")
    if not isinstance(etag, str) or not etag:
        raise DeletionUnavailableError("Missing concurrency token")
    return etag


def require_protocol_record(raw: Any, kind: str) -> None:
    if (
        not isinstance(raw, Mapping)
        or raw.get("kind") != kind
        or type(raw.get("deletionProtocol")) is not int
        or raw.get("deletionProtocol") != 1
        or not isinstance(raw.get("deletionEpoch"), str)
        or not raw["deletionEpoch"].strip()
        or raw.get("ttl") != -1
    ):
        raise DeletionIntegrityError("Invalid conversation coordination record")


def session_token(response: Any) -> str:
    # CosmosDict/CosmosList expose request-specific headers; the client's global
    # last_response_headers can belong to an unrelated concurrent request.
    read_headers = getattr(response, "get_response_headers", None)
    if not callable(read_headers):
        raise DeletionUnavailableError("Missing request-specific consistency headers")
    headers = read_headers()
    if not isinstance(headers, Mapping):
        raise DeletionUnavailableError("Invalid request-specific consistency headers")
    token = headers.get("x-ms-session-token")
    if not isinstance(token, str) or not token:
        raise DeletionUnavailableError("Missing partition consistency token")
    return token


def batch_failed_at(exc: Any, index: int, *statuses: int) -> bool:
    return exc.error_index == index and (
        exc.status_code in statuses
        or any(
            response.get("statusCode") in statuses
            for position, response in enumerate(exc.operation_responses or [])
            if position == index and isinstance(response, dict)
        )
    )


class CosmosDeletionMixin:
    _sessions: Any
    _messages: Any
    _documents: Any
    _client: Any
    _deletion_enabled: bool = False
    _deletion_rollout_id: str = ""
    _attachment_storage_required: bool = False
    _attachment_storage_id: str | None = None

    async def check_deletion_ready(self) -> None:
        if not self._deletion_enabled:
            return
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            raw = await self._sessions.read_item(
                item=self._deletion_rollout_id, partition_key=CONTROL_PARTITION
            )
            approval = RolloutApproval.model_validate(raw)
        except (CosmosResourceNotFoundError, ValidationError) as exc:
            raise DeletionUnavailableError("Approved deletion rollout is required") from exc
        if (
            approval.id != self._deletion_rollout_id
            or not approval.writerCutoverEvidence.strip()
            or not approval.recoveryReviewEvidence.strip()
        ):
            raise DeletionUnavailableError("Deletion rollout evidence does not match")
        # The aio facade exposes the account read on its connection, unlike the
        # sync facade. No account/container creation or management-plane call.
        account = await self._client.client_connection.GetDatabaseAccount()
        if (
            len(account.WritableLocations) != 1
            or getattr(account, "_EnableMultipleWritableLocations", None) is not False
            or (account.ConsistencyPolicy or {}).get("defaultConsistencyLevel")
            != "Session"
        ):
            raise DeletionUnavailableError("Single-write-region session consistency is required")
        for container, path in (
            (self._sessions, "/userId"),
            (self._messages, "/sessionId"),
            (self._documents, "/sessionId"),
        ):
            properties = await container.read()
            if (
                properties.get("partitionKey", {}).get("paths") != [path]
                or properties.get("defaultTtl") not in (None, -1)
                or properties.get("analyticalStorageTtl") not in (None, 0)
            ):
                raise DeletionUnavailableError("Deletion container layout/retention is incompatible")

    @staticmethod
    def _assert_active(raw: Any, user_id: str, session_id: str) -> None:
        if not isinstance(raw, Mapping):
            raise DeletionUnavailableError("Invalid conversation response")
        if (
            raw.get("id") != session_id
            or raw.get("userId") != user_id
            or raw.get("kind") in {"session_tombstone_v1", "session_initializing_v1"}
        ):
            raise SessionNotFoundError(session_id)
        if "deletionProtocol" in raw or "kind" in raw:
            require_protocol_record(raw, "session_v1")

    async def _read_session_raw(self, user_id: str, session_id: str) -> dict[str, Any]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            raw = await self._sessions.read_item(item=session_id, partition_key=user_id)
        except CosmosResourceNotFoundError as exc:
            raise SessionNotFoundError(session_id) from exc
        if not isinstance(raw, Mapping):
            raise DeletionUnavailableError("Invalid conversation observation")
        if raw.get("id") != session_id or raw.get("userId") != user_id:
            raise SessionNotFoundError(session_id)
        return dict(raw)

    async def _active_raw(self, user_id: str, session_id: str) -> dict[str, Any]:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

        for _ in range(CAS_ATTEMPTS):
            raw = await self._read_session_raw(user_id, session_id)
            self._assert_active(raw, user_id, session_id)
            if raw.get("deletionProtocol") != 1:
                return raw
            try:
                # A session-consistent read by a different replica may be stale.
                # This server-side CAS is the access linearization point.
                saved = await self._sessions.patch_item(
                    item=session_id, partition_key=user_id,
                    patch_operations=[
                        {"op": "set", "path": "/deletionProtocol", "value": 1}
                    ],
                    etag=require_etag(raw), match_condition=MatchConditions.IfNotModified,
                )
                self._assert_active(saved, user_id, session_id)
                if saved.get("deletionEpoch") != raw["deletionEpoch"]:
                    raise DeletionIntegrityError("Conversation generation changed")
                return saved
            except CosmosAccessConditionFailedError:
                continue
        raise DeletionUnavailableError("Conversation access could not be established")

    @staticmethod
    def _fence_body(session: Session, *, closed: bool) -> dict[str, Any]:
        return {
            "id": FENCE_ID, "sessionId": session.id, "userId": session.userId,
            "kind": "session_fence_v1", "epoch": session.deletionEpoch,
            "closed": closed, "ttl": -1,
        }

    @staticmethod
    def _assert_fence(raw: Any, session: Session) -> None:
        if (
            not isinstance(raw, Mapping)
            or raw.get("id") != FENCE_ID
            or raw.get("kind") != "session_fence_v1"
            or raw.get("sessionId") != session.id
            or raw.get("userId") != session.userId
            or raw.get("epoch") != session.deletionEpoch
            or type(raw.get("closed")) is not bool
            or raw.get("ttl") != -1
        ):
            raise DeletionIntegrityError("Conversation fence does not match")

    async def _create_fenced_session(
        self, session: Session, raw: dict[str, Any]
    ) -> Session:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceExistsError,
        )

        session.deletionProtocol = 1
        session.deletionEpoch = str(uuid4())
        session.attachmentStorageRequired = self._attachment_storage_required
        session.attachmentStorageId = (
            self._attachment_storage_id if self._attachment_storage_required else None
        )
        raw.update({
            "deletionProtocol": 1, "deletionEpoch": session.deletionEpoch,
            "attachmentStorageRequired": session.attachmentStorageRequired,
            "attachmentStorageId": session.attachmentStorageId,
            "kind": "session_v1", "ttl": -1,
        })
        reservation = InitializationRecord(
            id=session.id, userId=session.userId, deletionEpoch=session.deletionEpoch,
            kind="session_initializing_v1",
            createdAt=session.createdAt,
            attachmentStorageRequired=session.attachmentStorageRequired,
            attachmentStorageId=session.attachmentStorageId,
        )
        created = await self._sessions.create_item(reservation.model_dump(mode="json"))
        require_protocol_record(created, "session_initializing_v1")
        try:
            confirmed = InitializationRecord.model_validate(created)
        except ValidationError as exc:
            raise DeletionUnavailableError("Initialization reservation was not acknowledged") from exc
        if confirmed != reservation:
            raise DeletionIntegrityError("Initialization reservation does not match")
        original_etag = require_etag(created)
        for container in (self._messages, self._documents):
            try:
                fence = await container.create_item(self._fence_body(session, closed=False))
                self._assert_fence(fence, session)
                if fence["closed"]:
                    raise DeletionIntegrityError("Initialization fence is closed")
                require_etag(fence)
            except CosmosResourceExistsError as exc:
                # Never repair or reopen an existing fence, including a CLOSED
                # sentinel created by a concurrent deletion before initialization.
                raise DeletionIntegrityError("Conversation fence already exists") from exc
        try:
            published = await self._sessions.replace_item(
                item=session.id, body=raw, etag=original_etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosAccessConditionFailedError as exc:
            raise SessionNotFoundError(session.id) from exc
        self._assert_active(published, session.userId, session.id)
        if published.get("deletionEpoch") != session.deletionEpoch:
            raise DeletionIntegrityError("Published conversation generation does not match")
        require_etag(published)
        return session

    async def list_initializations(
        self, user_id: str, cursor: str = ""
    ) -> InitializationPage:
        after = initialization_after(cursor)
        rows = [
            raw async for raw in self._sessions.query_items(
                query=(
                    f"SELECT TOP {STATUS_ITEMS + 1} * FROM c WHERE c.userId = @uid "
                    "AND c.kind = 'session_initializing_v1' AND c.id > @cursor ORDER BY c.id"
                ),
                parameters=[
                    {"name": "@uid", "value": user_id},
                    {"name": "@cursor", "value": after},
                ],
                partition_key=user_id, max_item_count=STATUS_ITEMS + 1,
            )
        ]
        try:
            for raw in rows:
                require_protocol_record(raw, "session_initializing_v1")
            records = [InitializationRecord.model_validate(raw) for raw in rows]
        except ValidationError as exc:
            raise DeletionIntegrityError("Invalid initialization observation") from exc
        if any(record.userId != user_id for record in records):
            raise DeletionIntegrityError("Initialization ownership mismatch")
        more = len(records) > STATUS_ITEMS
        shown = records[:STATUS_ITEMS]
        return InitializationPage(
            items=[
                SessionInitialization(sessionId=record.id, createdAt=record.createdAt)
                for record in shown
            ],
            hasMore=more,
            nextCursor=initialization_cursor(shown[-1].id) if more else None,
        )

    async def _fenced_batch(
        self, container: Any, session: Session, operations: list[BatchOperation]
    ) -> Any:
        from azure.cosmos.exceptions import (
            CosmosBatchOperationError,
            CosmosResourceNotFoundError,
        )

        for _ in range(CAS_ATTEMPTS):
            try:
                fence = await container.read_item(
                    item=FENCE_ID, partition_key=session.id
                )
            except CosmosResourceNotFoundError as exc:
                raise DeletionIntegrityError("Conversation fence is missing") from exc
            self._assert_fence(fence, session)
            if fence["closed"]:
                raise SessionNotFoundError(session.id)
            try:
                # The SDK consumes operation options while formatting a batch.
                # Fence retries must retain each child's original precondition.
                return await container.execute_item_batch(
                    partition_key=session.id,
                    batch_operations=[
                        ("replace", (FENCE_ID, body_only(fence)),
                         {"if_match_etag": require_etag(fence)}),
                        *((operation, args, dict(options)) for operation, args, options in operations),
                    ],
                )
            except CosmosBatchOperationError as exc:
                if batch_failed_at(exc, 0, 412):
                    continue
                if batch_failed_at(exc, 0, 404):
                    raise DeletionIntegrityError("Conversation fence vanished") from exc
                raise
        raise DeletionUnavailableError("Conversation write fence is contended")

    async def begin_deletion(self, user_id: str, session_id: str) -> DeletionStatus:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

        if not self._deletion_enabled:
            raise DeletionDisabledError()
        for _ in range(CAS_ATTEMPTS):
            raw = await self._read_session_raw(user_id, session_id)
            if raw.get("kind") == "session_tombstone_v1":
                require_protocol_record(raw, "session_tombstone_v1")
                return DeletionRecord.model_validate(raw).status
            if type(raw.get("deletionProtocol")) is not int or raw.get("deletionProtocol") != 1:
                if "kind" in raw or "deletionProtocol" in raw:
                    raise DeletionIntegrityError("Invalid conversation protocol marker")
                raise DeletionMigrationRequiredError()
            if raw.get("kind") not in {"session_v1", "session_initializing_v1"}:
                raise DeletionIntegrityError("Unsupported conversation state")
            require_protocol_record(raw, raw["kind"])
            record = DeletionRecord(
                id=session_id, userId=user_id, deletionEpoch=raw["deletionEpoch"],
                attachmentStorageRequired=raw.get("attachmentStorageRequired", False),
                attachmentStorageId=raw.get("attachmentStorageId"),
                status=DeletionStatus(sessionId=session_id),
            )
            try:
                await self._sessions.replace_item(
                    item=session_id, body=record.model_dump(mode="json"),
                    etag=require_etag(raw), match_condition=MatchConditions.IfNotModified,
                )
                return record.status
            except CosmosAccessConditionFailedError:
                continue
        raise DeletionUnavailableError("Deletion intent could not be committed")

    async def get_deletion_status(self, user_id: str, session_id: str) -> DeletionStatus:
        raw = await self._read_session_raw(user_id, session_id)
        if raw.get("kind") != "session_tombstone_v1":
            raise SessionNotFoundError(session_id)
        require_protocol_record(raw, "session_tombstone_v1")
        return DeletionRecord.model_validate(raw).status

    async def list_deletions(self, user_id: str, cursor: str = "") -> DeletionPage:
        # Keyset pagination is read-only and never doubles as a job runner.
        rows = [
            raw async for raw in self._sessions.query_items(
                query=(
                    f"SELECT TOP {STATUS_ITEMS + 1} * FROM c WHERE c.userId = @uid "
                    "AND c.kind = 'session_tombstone_v1' AND c.id > @cursor ORDER BY c.id"
                ),
                parameters=[
                    {"name": "@uid", "value": user_id},
                    {"name": "@cursor", "value": cursor},
                ],
                partition_key=user_id, max_item_count=STATUS_ITEMS + 1,
            )
        ]
        for row in rows:
            require_protocol_record(row, "session_tombstone_v1")
        records = [DeletionRecord.model_validate(row) for row in rows[:STATUS_ITEMS]]
        if any(record.userId != user_id for record in records):
            raise DeletionIntegrityError("Deletion ownership mismatch")
        more = len(rows) > STATUS_ITEMS
        return DeletionPage(
            items=[record.status for record in records], hasMore=more,
            nextCursor=records[-1].id if more else None,
        )

    async def claim_deletion(self, user_id: str, session_id: str) -> DeletionLease | None:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

        if not self._deletion_enabled:
            raise DeletionDisabledError()
        for _ in range(CAS_ATTEMPTS):
            raw = await self._read_session_raw(user_id, session_id)
            if raw.get("kind") != "session_tombstone_v1":
                raise SessionNotFoundError(session_id)
            require_protocol_record(raw, "session_tombstone_v1")
            record = DeletionRecord.model_validate(raw)
            now = now_utc()
            if record.status.state == "cleanup_verified" or (
                record.leaseToken and record.leaseExpiresAt and record.leaseExpiresAt > now
            ):
                return None
            token = str(uuid4())
            record.leaseToken = token
            record.leaseExpiresAt = now + timedelta(seconds=LEASE_SECONDS)
            record.status.attempts += 1
            record.status.state = "pending"
            record.status.retryReason = None
            record.status.updatedAt = now
            try:
                saved = await self._sessions.replace_item(
                    item=session_id, body=record.model_dump(mode="json"),
                    etag=require_etag(raw), match_condition=MatchConditions.IfNotModified,
                )
                return DeletionLease(record=record, etag=require_etag(saved), token=token)
            except CosmosAccessConditionFailedError:
                continue
        raise DeletionUnavailableError("Deletion lease is contended")

    async def checkpoint_deletion(
        self, lease: DeletionLease, status: DeletionStatus, *, release: bool
    ) -> DeletionLease:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

        if lease.record.leaseToken != lease.token:
            raise DeletionIntegrityError("Deletion lease does not match")
        record = lease.record.model_copy(deep=True)
        record.status = status.model_copy(deep=True)
        record.status.updatedAt = now_utc()
        if release:
            record.leaseToken = None
            record.leaseExpiresAt = None
        try:
            saved = await self._sessions.replace_item(
                item=record.id, body=record.model_dump(mode="json"),
                etag=lease.etag, match_condition=MatchConditions.IfNotModified,
            )
        except CosmosAccessConditionFailedError as exc:
            raise DeletionUnavailableError("Deletion lease changed") from exc
        return DeletionLease(record=record, etag=require_etag(saved), token=lease.token)

    @staticmethod
    def _deleting_session(lease: DeletionLease) -> Session:
        return Session(
            id=lease.record.id, userId=lease.record.userId,
            deletionProtocol=1, deletionEpoch=lease.record.deletionEpoch,
        )

    async def _closed_fence_barrier(self, container: Any, lease: DeletionLease) -> str:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceExistsError,
            CosmosResourceNotFoundError,
        )

        session = self._deleting_session(lease)
        for _ in range(CAS_ATTEMPTS):
            try:
                raw = await container.read_item(item=FENCE_ID, partition_key=session.id)
            except CosmosResourceNotFoundError:
                try:
                    saved = await container.create_item(self._fence_body(session, closed=True))
                    return session_token(saved)
                except CosmosResourceExistsError:
                    continue
            self._assert_fence(raw, session)
            etag = require_etag(raw)
            raw = body_only(raw) | {"closed": True}
            try:
                saved = await container.replace_item(
                    item=FENCE_ID, body=raw, etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                )
                return session_token(saved)
            except CosmosAccessConditionFailedError:
                continue
        raise DeletionUnavailableError("Closed fence could not be established")

    async def close_deletion_fences(self, lease: DeletionLease) -> None:
        await self._closed_fence_barrier(self._messages, lease)
        await self._closed_fence_barrier(self._documents, lease)

    async def cleanup_child_page(
        self, lease: DeletionLease, *, documents: bool
    ) -> bool:
        """Remove at most CLEANUP_ITEMS rows. False means bounded work remains."""
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        container = self._documents if documents else self._messages
        token = await self._closed_fence_barrier(container, lease)
        extra = " AND (NOT IS_DEFINED(c.kind) OR c.kind != 'session_upload_v1')" if documents else ""
        rows = [
            raw async for raw in container.query_items(
                query=(
                    f"SELECT TOP {CLEANUP_ITEMS} * FROM c WHERE c.sessionId = @sid "
                    "AND c.id != @fence" + extra
                ),
                parameters=[
                    {"name": "@sid", "value": lease.record.id},
                    {"name": "@fence", "value": FENCE_ID},
                ],
                partition_key=lease.record.id, session_token=token,
                max_item_count=CLEANUP_ITEMS,
            )
        ]
        for raw in rows:
            if (
                raw.get("userId") != lease.record.userId
                or raw.get("sessionId") != lease.record.id
                or not isinstance(raw.get("id"), str)
                or raw["id"].startswith("__ai4ia_")
                or raw.get("kind") is not None
            ):
                raise DeletionIntegrityError("Unexpected conversation child")
        for raw in rows:
            try:
                await container.delete_item(item=raw["id"], partition_key=lease.record.id)
            except CosmosResourceNotFoundError:
                pass
        # An empty response is evidence only with the explicit partition token
        # from the server-committed CLOSED fence above.
        return not rows

    async def deletion_uploads(
        self, lease: DeletionLease, *, unsettled_only: bool
    ) -> list[UploadIntent]:
        token = await self._closed_fence_barrier(self._documents, lease)
        extra = " AND c.settled = false" if unsettled_only else ""
        rows = [
            raw async for raw in self._documents.query_items(
                query=(
                    f"SELECT TOP {CLEANUP_ITEMS + 1} * FROM c WHERE c.sessionId = @sid "
                    "AND c.kind = 'session_upload_v1'" + extra
                ),
                parameters=[{"name": "@sid", "value": lease.record.id}],
                partition_key=lease.record.id, session_token=token,
                max_item_count=CLEANUP_ITEMS + 1,
            )
        ]
        intents = [UploadIntent.model_validate(raw) for raw in rows]
        for intent in intents:
            if (
                intent.userId != lease.record.userId or intent.sessionId != lease.record.id
                or intent.epoch != lease.record.deletionEpoch
                or not intent.id.startswith(UPLOAD_ID_PREFIX)
            ):
                raise DeletionIntegrityError("Upload intent does not match")
        return intents

    async def remove_settled_uploads(
        self, lease: DeletionLease, intents: list[UploadIntent]
    ) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        for intent in intents[:CLEANUP_ITEMS]:
            if (
                not intent.settled or intent.userId != lease.record.userId
                or intent.sessionId != lease.record.id
                or intent.epoch != lease.record.deletionEpoch
                or not intent.id.startswith(UPLOAD_ID_PREFIX)
            ):
                raise DeletionIntegrityError("Unsettled upload cannot be forgotten")
            try:
                await self._documents.delete_item(item=intent.id, partition_key=lease.record.id)
            except CosmosResourceNotFoundError:
                pass

    async def reserve_attachment_upload(
        self, user_id: str, session_id: str, document_id: str, *, storage_id: str
    ) -> UploadIntent | None:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

        self._assert_child_id(document_id)
        for attempt in range(CAS_ATTEMPTS):
            raw = await self._active_raw(user_id, session_id)
            session = Session.model_validate(raw)
            if session.deletionProtocol != 1:
                return None
            if session.attachmentStorageId not in (None, storage_id):
                raise DeletionIntegrityError("Conversation attachment store changed")
            try:
                # Persist the exact target before starting any upload. Later
                # config changes cannot make a different empty container pass.
                await self._sessions.patch_item(
                    item=session_id, partition_key=user_id,
                    patch_operations=[
                        {"op": "set", "path": "/attachmentStorageRequired", "value": True},
                        {"op": "set", "path": "/attachmentStorageId", "value": storage_id},
                    ],
                    etag=require_etag(raw), match_condition=MatchConditions.IfNotModified,
                )
                break
            except CosmosAccessConditionFailedError:
                if attempt == CAS_ATTEMPTS - 1:
                    raise DeletionUnavailableError("Upload target could not be recorded") from None
        intent = UploadIntent(
            id=UPLOAD_ID_PREFIX + str(uuid4()), sessionId=session_id, userId=user_id,
            epoch=session.deletionEpoch or "", documentId=document_id,
        )
        await self._fenced_batch(
            self._documents, session, [("create", (intent.model_dump(mode="json"),), {})]
        )
        return intent

    async def settle_attachment_upload(self, intent: UploadIntent) -> None:
        """Only called after Blob PUT conclusively returns, including post-delete.

        This is a maintenance transition on an existing attempt, not permission
        to write another blob or recreate a document. It cannot create a ticket.
        """
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

        for _ in range(CAS_ATTEMPTS):
            raw = await self._documents.read_item(item=intent.id, partition_key=intent.sessionId)
            current = UploadIntent.model_validate(raw)
            if current.model_copy(update={"settled": False}) != intent.model_copy(
                update={"settled": False}
            ):
                raise DeletionIntegrityError("Upload completion does not match its intent")
            if current.settled:
                return
            current.settled = True
            try:
                await self._documents.replace_item(
                    item=intent.id, body=current.model_dump(mode="json"),
                    etag=require_etag(raw), match_condition=MatchConditions.IfNotModified,
                )
                return
            except CosmosAccessConditionFailedError:
                continue
        raise DeletionUnavailableError("Upload completion could not be recorded")

    @staticmethod
    def _assert_child_id(item_id: str) -> None:
        if item_id.startswith("__ai4ia_"):
            raise DeletionIntegrityError("Reserved conversation item id")

    async def _fenced_delete(
        self, container: Any, session: Session, item_id: str
    ) -> None:
        from azure.cosmos.exceptions import (
            CosmosBatchOperationError,
            CosmosResourceNotFoundError,
        )

        self._assert_child_id(item_id)
        for _ in range(CAS_ATTEMPTS):
            try:
                raw = await container.read_item(item=item_id, partition_key=session.id)
            except CosmosResourceNotFoundError:
                return
            if raw.get("userId") != session.userId or raw.get("sessionId") != session.id:
                raise DeletionIntegrityError("Conversation child ownership mismatch")
            try:
                await self._fenced_batch(
                    container, session,
                    [("delete", (item_id,), {"if_match_etag": require_etag(raw)})],
                )
                return
            except CosmosBatchOperationError as exc:
                if batch_failed_at(exc, 1, 404):
                    return
                if batch_failed_at(exc, 1, 412):
                    continue
                raise
        raise DeletionUnavailableError("Conversation child delete is contended")
