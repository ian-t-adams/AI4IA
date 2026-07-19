# pyright: reportArgumentType=false, reportCallIssue=false
# ^ Azure Cosmos SDK typing friction, not real defects: container.query_items's
#   `parameters` is typed list[dict[str, object]], but our list[dict[str, str]]
#   literals are rejected by list/dict invariance, which also makes the query_items
#   overloads fail to resolve. The queries are correct at runtime. Scoped to this
#   Cosmos repo module so the rules stay active everywhere else.
"""Cosmos DB (NoSQL) SessionRepository using AAD (managed identity) auth.

Containers (created by infra/modules/data.bicep):
- ``sessions``  PK ``/userId``
- ``messages``  PK ``/sessionId`` (userId denormalized + ownership-checked)

Azure SDKs are imported lazily so the app and tests run without them installed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from .models import (
    ActivityStep,
    Document,
    Message,
    MessageAttachment,
    MessageRole,
    MessageStatus,
    Session,
    normalize_session_patch_changes,
    normalize_session_title,
    turn_message_id,
)
from .repository import (
    ClientTurnConflictError,
    SessionConflictError,
    SessionNotFoundError,
)


class CosmosSessionRepository:
    def __init__(self, endpoint: str, database: str) -> None:
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=self._credential)
        db = self._client.get_database_client(database)
        self._sessions = db.get_container_client("sessions")
        self._messages = db.get_container_client("messages")
        self._documents = db.get_container_client("documents")

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()

    @staticmethod
    def _to_doc(model: Session | Message | Document) -> dict[str, Any]:
        doc = model.model_dump(mode="json")
        if isinstance(model, Message) and model.clientRequestFingerprint is not None:
            doc["clientRequestFingerprint"] = model.clientRequestFingerprint
        if isinstance(model, Message) and model.claimLeaseId is not None:
            doc["claimLeaseId"] = model.claimLeaseId
        return doc

    async def _owned_session(self, user_id: str, session_id: str) -> Session:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            doc = await self._sessions.read_item(item=session_id, partition_key=user_id)
        except CosmosResourceNotFoundError as exc:
            raise SessionNotFoundError(session_id) from exc
        return Session.model_validate(doc)

    async def create_session(self, session: Session) -> Session:
        await self._sessions.create_item(self._to_doc(session))
        return session

    async def get_session(self, user_id: str, session_id: str) -> Session:
        return await self._owned_session(user_id, session_id)

    async def list_sessions(self, user_id: str) -> list[Session]:
        query = "SELECT * FROM c WHERE c.userId = @uid ORDER BY c.updatedAt DESC"
        params = [{"name": "@uid", "value": user_id}]
        items = [
            Session.model_validate(doc)
            async for doc in self._sessions.query_items(query=query, parameters=params)
        ]
        return items

    async def update_session(self, session: Session) -> Session:
        raise SessionConflictError(
            "Unversioned full session replacement is disabled; use patch_session."
        )

    async def patch_session(
        self, user_id: str, session_id: str, changes: dict[str, object]
    ) -> Session:
        await self._owned_session(user_id, session_id)
        normalized = normalize_session_patch_changes(changes)
        operations = [
            {
                "op": "set",
                "path": f"/{field_name}",
                "value": (
                    value.model_dump(mode="json")
                    if isinstance(value, BaseModel)
                    else value
                ),
            }
            for field_name, value in normalized.items()
        ]
        operations.append(
            {
                "op": "set",
                "path": "/updatedAt",
                "value": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
        await self._sessions.patch_item(
            item=session_id,
            partition_key=user_id,
            patch_operations=operations,
        )
        return await self._owned_session(user_id, session_id)

    async def set_generated_title_if_eligible(
        self, user_id: str, session_id: str, title: str
    ) -> bool:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceNotFoundError,
        )

        normalized = normalize_session_title(title)
        for _attempt in range(3):
            try:
                raw = await self._sessions.read_item(
                    item=session_id, partition_key=user_id
                )
            except CosmosResourceNotFoundError as exc:
                raise SessionNotFoundError(session_id) from exc
            if raw.get("title", "New chat") != "New chat":
                return False
            if raw.get("titleSource", "auto") == "manual":
                return False
            try:
                await self._sessions.patch_item(
                    item=session_id,
                    partition_key=user_id,
                    patch_operations=[
                        {"op": "set", "path": "/title", "value": normalized},
                        {"op": "set", "path": "/titleSource", "value": "auto"},
                        {
                            "op": "set",
                            "path": "/updatedAt",
                            "value": datetime.now(timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z"),
                        },
                    ],
                    etag=raw.get("_etag"),
                    match_condition=MatchConditions.IfNotModified,
                )
                return True
            except CosmosAccessConditionFailedError:
                continue
        raise SessionConflictError(session_id)

    async def mutate_library_document_ids(
        self,
        user_id: str,
        session_id: str,
        document_id: str,
        *,
        add: bool,
        legacy_ids: list[str] | None = None,
    ) -> Session:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

        for _attempt in range(3):
            try:
                raw = await self._sessions.read_item(
                    item=session_id, partition_key=user_id
                )
            except Exception as exc:
                from azure.cosmos.exceptions import CosmosResourceNotFoundError

                if isinstance(exc, CosmosResourceNotFoundError):
                    raise SessionNotFoundError(session_id) from exc
                raise
            current = raw.get("libraryDocumentIds")
            if current is None:
                if add:
                    return Session.model_validate(raw)
                values = list(legacy_ids or [])
            else:
                values = list(current)
            if add and document_id not in values:
                values.append(document_id)
            elif not add:
                values = [value for value in values if value != document_id]
            try:
                await self._sessions.patch_item(
                    item=session_id,
                    partition_key=user_id,
                    patch_operations=[
                        {
                            "op": "set",
                            "path": "/libraryDocumentIds",
                            "value": values,
                        },
                        {
                            "op": "set",
                            "path": "/updatedAt",
                            "value": datetime.now(timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z"),
                        },
                    ],
                    etag=raw.get("_etag"),
                    match_condition=MatchConditions.IfNotModified,
                )
                return await self._owned_session(user_id, session_id)
            except CosmosAccessConditionFailedError:
                continue
        raise SessionConflictError(session_id)

    async def _delete_summary_replies_before(
        self, session_id: str, version: int
    ) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        query = (
            "SELECT c.id FROM c WHERE c.sessionId = @sid "
            "AND IS_DEFINED(c.summaryVersion) AND c.summaryVersion < @version "
            "AND (NOT IS_DEFINED(c.clientTurnId) OR IS_NULL(c.clientTurnId))"
        )
        params = [
            {"name": "@sid", "value": session_id},
            {"name": "@version", "value": version},
        ]
        async for item in self._messages.query_items(
            query=query, parameters=params, partition_key=session_id
        ):
            try:
                await self._messages.delete_item(
                    item=item["id"], partition_key=session_id
                )
            except CosmosResourceNotFoundError:
                pass

    async def invalidate_summary(
        self, user_id: str, session_id: str
    ) -> Session:
        result = await self._mutate_summary(
            user_id,
            session_id,
            expected_version=None,
            summary=None,
            summarized_through_message_id=None,
        )
        assert result is not None
        return result

    async def commit_summary_if_version(
        self,
        user_id: str,
        session_id: str,
        *,
        expected_version: int,
        summary: str,
        summarized_through_message_id: str,
    ) -> Session | None:
        return await self._mutate_summary(
            user_id,
            session_id,
            expected_version=expected_version,
            summary=summary,
            summarized_through_message_id=summarized_through_message_id,
        )

    async def _mutate_summary(
        self,
        user_id: str,
        session_id: str,
        *,
        expected_version: int | None,
        summary: str | None,
        summarized_through_message_id: str | None,
    ) -> Session | None:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceNotFoundError,
        )

        for _attempt in range(3):
            try:
                raw = await self._sessions.read_item(
                    item=session_id, partition_key=user_id
                )
            except CosmosResourceNotFoundError as exc:
                raise SessionNotFoundError(session_id) from exc
            version = int(raw.get("summaryVersion") or 0)
            if expected_version is not None and version != expected_version:
                return None
            next_version = version + 1
            try:
                await self._sessions.patch_item(
                    item=session_id,
                    partition_key=user_id,
                    patch_operations=[
                        {"op": "set", "path": "/summary", "value": summary},
                        {
                            "op": "set",
                            "path": "/summarizedThroughMessageId",
                            "value": summarized_through_message_id,
                        },
                        {
                            "op": "set",
                            "path": "/summaryVersion",
                            "value": next_version,
                        },
                        {
                            "op": "set",
                            "path": "/updatedAt",
                            "value": datetime.now(timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z"),
                        },
                    ],
                    etag=raw.get("_etag"),
                    match_condition=MatchConditions.IfNotModified,
                )
                committed = await self._owned_session(user_id, session_id)
                await self._delete_summary_replies_before(
                    session_id, committed.summaryVersion
                )
                return committed
            except CosmosAccessConditionFailedError:
                continue
        raise SessionConflictError(session_id)

    async def touch_session(self, user_id: str, session_id: str) -> None:
        await self._owned_session(user_id, session_id)
        updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        await self._sessions.patch_item(
            item=session_id,
            partition_key=user_id,
            patch_operations=[
                {"op": "set", "path": "/updatedAt", "value": updated_at}
            ],
        )

    async def delete_session(self, user_id: str, session_id: str) -> None:
        await self._owned_session(user_id, session_id)
        # Delete child messages first (partition = sessionId).
        query = "SELECT c.id FROM c WHERE c.sessionId = @sid"
        params = [{"name": "@sid", "value": session_id}]
        async for doc in self._messages.query_items(
            query=query, parameters=params, partition_key=session_id
        ):
            await self._messages.delete_item(item=doc["id"], partition_key=session_id)
        # Cascade-delete uploaded documents (also partitioned by sessionId).
        async for doc in self._documents.query_items(
            query=query, parameters=params, partition_key=session_id
        ):
            await self._documents.delete_item(item=doc["id"], partition_key=session_id)
        await self._sessions.delete_item(item=session_id, partition_key=user_id)

    async def add_message(self, user_id: str, message: Message) -> Message:
        await self._owned_session(user_id, message.sessionId)
        message.userId = user_id
        await self._messages.create_item(self._to_doc(message))
        return message

    async def claim_chat_turn(
        self, user_id: str, user_message: Message, assistant_message: Message
    ) -> tuple[Message, Message, bool]:
        from azure.cosmos.exceptions import (
            CosmosBatchOperationError,
            CosmosResourceNotFoundError,
        )

        await self._owned_session(user_id, user_message.sessionId)
        if (
            user_message.sessionId != assistant_message.sessionId
            or not user_message.clientTurnId
            or user_message.clientTurnId != assistant_message.clientTurnId
        ):
            raise ValueError("A chat turn must have one session and clientTurnId.")
        user_message.userId = user_id
        assistant_message.userId = user_id
        try:
            await self._messages.execute_item_batch(
                [
                    ("create", (self._to_doc(user_message),)),
                    ("create", (self._to_doc(assistant_message),)),
                ],
                partition_key=user_message.sessionId,
            )
            return user_message, assistant_message, True
        except CosmosBatchOperationError:
            try:
                user_doc = await self._messages.read_item(
                    item=user_message.id, partition_key=user_message.sessionId
                )
                assistant_doc = await self._messages.read_item(
                    item=assistant_message.id, partition_key=user_message.sessionId
                )
            except CosmosResourceNotFoundError as exc:
                raise ClientTurnConflictError(user_message.clientTurnId) from exc
            saved_user = Message.model_validate(user_doc)
            saved_assistant = Message.model_validate(assistant_doc)
            if (
                saved_user.userId != user_id
                or saved_assistant.userId != user_id
                or saved_user.clientTurnId != user_message.clientTurnId
                or saved_assistant.clientTurnId != user_message.clientTurnId
                or saved_user.clientRequestFingerprint
                != user_message.clientRequestFingerprint
                or saved_assistant.clientRequestFingerprint
                != assistant_message.clientRequestFingerprint
            ):
                raise ClientTurnConflictError(user_message.clientTurnId)
            return saved_user, saved_assistant, False

    async def get_chat_turn(
        self,
        user_id: str,
        session_id: str,
        client_turn_id: str,
        fingerprint: str,
    ) -> tuple[Message, Message] | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        await self._owned_session(user_id, session_id)
        ids = {
            role: turn_message_id(user_id, session_id, client_turn_id, role)
            for role in (MessageRole.user, MessageRole.assistant)
        }
        raw: dict[MessageRole, dict[str, Any]] = {}
        for role, message_id in ids.items():
            try:
                raw[role] = await self._messages.read_item(
                    item=message_id, partition_key=session_id
                )
            except CosmosResourceNotFoundError:
                continue
        if not raw:
            return None
        if len(raw) != 2:
            raise ClientTurnConflictError(client_turn_id)
        saved_user = Message.model_validate(raw[MessageRole.user])
        saved_assistant = Message.model_validate(raw[MessageRole.assistant])
        if (
            saved_user.userId != user_id
            or saved_assistant.userId != user_id
            or saved_user.clientTurnId != client_turn_id
            or saved_assistant.clientTurnId != client_turn_id
            or saved_user.clientRequestFingerprint != fingerprint
            or saved_assistant.clientRequestFingerprint != fingerprint
        ):
            raise ClientTurnConflictError(client_turn_id)
        return saved_user, saved_assistant

    async def terminalize_chat_turn(
        self,
        user_id: str,
        session_id: str,
        assistant_message_id: str,
        *,
        status: MessageStatus,
        content: str,
        expected_claim_lease_id: str | None = None,
        stale_before: datetime | None = None,
        steps: list[ActivityStep] | None = None,
        attachments: list[MessageAttachment] | None = None,
        summary_version: int | None = None,
    ) -> Message | None:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceNotFoundError,
        )

        if expected_claim_lease_id is None and stale_before is None:
            raise ValueError("A claim lease or stale cutoff is required.")
        await self._owned_session(user_id, session_id)
        for _ in range(3):
            try:
                raw = await self._messages.read_item(
                    item=assistant_message_id, partition_key=session_id
                )
            except CosmosResourceNotFoundError:
                return None
            current = Message.model_validate(raw)
            if current.userId != user_id:
                raise SessionNotFoundError(session_id)
            if current.status.value != "streaming":
                return current
            if (
                expected_claim_lease_id is not None
                and current.claimLeaseId != expected_claim_lease_id
            ):
                return current
            if stale_before is not None and current.createdAt > stale_before:
                return current
            operations = [
                {"op": "set", "path": "/status", "value": status.value},
                {"op": "set", "path": "/content", "value": content},
                {
                    "op": "set",
                    "path": "/attachments",
                    "value": [
                        attachment.model_dump(mode="json")
                        for attachment in attachments or []
                    ],
                },
                {
                    "op": "set",
                    "path": "/steps",
                    "value": (
                        [step.model_dump(mode="json") for step in steps]
                        if steps is not None
                        else None
                    ),
                },
            ]
            if summary_version is not None:
                operations.append(
                    {
                        "op": "set",
                        "path": "/summaryVersion",
                        "value": summary_version,
                    }
                )
            try:
                saved = await self._messages.patch_item(
                    item=assistant_message_id,
                    partition_key=session_id,
                    patch_operations=operations,
                    etag=raw.get("_etag"),
                    match_condition=MatchConditions.IfNotModified,
                )
                return Message.model_validate(saved)
            except CosmosAccessConditionFailedError:
                continue
        latest = await self._messages.read_item(
            item=assistant_message_id, partition_key=session_id
        )
        return Message.model_validate(latest)

    async def add_message_if_summary_version(
        self, user_id: str, message: Message, *, expected_version: int
    ) -> bool:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        if message.claimLeaseId is not None:
            raise ValueError("Claimed turns must use terminalize_chat_turn.")
        session = await self._owned_session(user_id, message.sessionId)
        if session.summaryVersion != expected_version:
            return False
        message.userId = user_id
        message.summaryVersion = expected_version
        await self._messages.upsert_item(self._to_doc(message))
        latest = await self._owned_session(user_id, message.sessionId)
        if latest.summaryVersion == expected_version:
            return True
        try:
            await self._messages.delete_item(
                item=message.id, partition_key=message.sessionId
            )
        except CosmosResourceNotFoundError:
            pass
        return False

    async def upsert_message(self, user_id: str, message: Message) -> Message:
        if message.claimLeaseId is not None:
            raise ValueError("Claimed turns must use terminalize_chat_turn.")
        await self._owned_session(user_id, message.sessionId)
        message.userId = user_id
        await self._messages.upsert_item(self._to_doc(message))
        return message

    async def list_messages(self, user_id: str, session_id: str) -> list[Message]:
        await self._owned_session(user_id, session_id)
        query = "SELECT * FROM c WHERE c.sessionId = @sid ORDER BY c.createdAt ASC"
        params = [{"name": "@sid", "value": session_id}]
        return [
            Message.model_validate(doc)
            async for doc in self._messages.query_items(query=query, parameters=params)
        ]

    async def clear_messages(self, user_id: str, session_id: str) -> None:
        await self._owned_session(user_id, session_id)
        query = "SELECT c.id FROM c WHERE c.sessionId = @sid"
        params = [{"name": "@sid", "value": session_id}]
        async for doc in self._messages.query_items(
            query=query, parameters=params, partition_key=session_id
        ):
            await self._messages.delete_item(item=doc["id"], partition_key=session_id)

    async def clear_messages_before(
        self,
        user_id: str,
        session_id: str,
        *,
        cutoff: datetime,
        preserve_ids: frozenset[str],
    ) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        await self._owned_session(user_id, session_id)
        query = (
            "SELECT c.id, c.clientTurnId, c.status FROM c WHERE c.sessionId = @sid "
            "AND c.createdAt <= @cutoff"
        )
        params = [
            {"name": "@sid", "value": session_id},
            {
                "name": "@cutoff",
                "value": cutoff.isoformat().replace("+00:00", "Z"),
            },
        ]
        candidates = [
            doc
            async for doc in self._messages.query_items(
                query=query, parameters=params, partition_key=session_id
            )
        ]
        streaming_turn_ids = {
            doc.get("clientTurnId")
            for doc in candidates
            if doc.get("status") == MessageStatus.streaming.value
            and doc.get("clientTurnId")
        }
        for doc in candidates:
            if doc["id"] in preserve_ids:
                continue
            if doc.get("clientTurnId") in streaming_turn_ids:
                continue
            try:
                await self._messages.delete_item(
                    item=doc["id"], partition_key=session_id
                )
            except CosmosResourceNotFoundError:
                pass

    async def add_document(self, user_id: str, document: Document) -> Document:
        await self._owned_session(user_id, document.sessionId)
        document.userId = user_id
        await self._documents.create_item(self._to_doc(document))
        return document

    async def list_documents(self, user_id: str, session_id: str) -> list[Document]:
        await self._owned_session(user_id, session_id)
        query = "SELECT * FROM c WHERE c.sessionId = @sid ORDER BY c.createdAt ASC"
        params = [{"name": "@sid", "value": session_id}]
        return [
            Document.model_validate(doc)
            async for doc in self._documents.query_items(
                query=query, parameters=params, partition_key=session_id
            )
        ]

    async def get_document(
        self, user_id: str, session_id: str, document_id: str
    ) -> Document | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        await self._owned_session(user_id, session_id)
        try:
            doc = await self._documents.read_item(
                item=document_id, partition_key=session_id
            )
        except CosmosResourceNotFoundError:
            return None
        document = Document.model_validate(doc)
        # Defense in depth: the partition already scopes to the owned session,
        # but never return a doc whose denormalized owner doesn't match.
        if document.userId != user_id:
            return None
        return document

    async def delete_document(
        self, user_id: str, session_id: str, document_id: str
    ) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        await self._owned_session(user_id, session_id)
        try:
            await self._documents.delete_item(
                item=document_id, partition_key=session_id
            )
        except CosmosResourceNotFoundError:
            return
