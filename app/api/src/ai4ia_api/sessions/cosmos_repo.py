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
    Document,
    Message,
    Session,
    normalize_session_patch_changes,
    normalize_session_title,
)
from .repository import SessionConflictError, SessionNotFoundError


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
        return model.model_dump(mode="json")

    async def _owned_session(self, user_id: str, session_id: str) -> Session:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            doc = await self._sessions.read_item(item=session_id, partition_key=user_id)
        except CosmosResourceNotFoundError as exc:
            raise SessionNotFoundError(session_id) from exc
        return Session.model_validate(doc)

    async def _patch_session_item(
        self,
        user_id: str,
        session_id: str,
        patch_operations: list[dict[str, Any]],
        *,
        etag: str | None = None,
    ) -> None:
        """``patch_item`` on the sessions container, not-found translated the
        same way every read in this repository already is.

        Every caller re-checks (or just read) the session before patching, but
        a concurrent delete can still land in the gap between that check and
        this call. Without translation the raw Cosmos 404 would reach the
        app's generic Azure-error handler and surface as a 500/503 instead of
        the session's normal 404. ``etag`` makes the patch conditional
        (``IfNotModified``) so a concurrent *edit* instead raises
        ``CosmosAccessConditionFailedError`` for the caller's own retry loop.
        """
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        kwargs: dict[str, Any] = {}
        if etag is not None:
            from azure.core import MatchConditions

            kwargs["etag"] = etag
            kwargs["match_condition"] = MatchConditions.IfNotModified
        try:
            await self._sessions.patch_item(
                item=session_id,
                partition_key=user_id,
                patch_operations=patch_operations,
                **kwargs,
            )
        except CosmosResourceNotFoundError as exc:
            raise SessionNotFoundError(session_id) from exc

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
        await self._patch_session_item(user_id, session_id, operations)
        return await self._owned_session(user_id, session_id)

    async def set_generated_title_if_eligible(
        self, user_id: str, session_id: str, title: str
    ) -> bool:
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
                await self._patch_session_item(
                    user_id,
                    session_id,
                    [
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
                await self._patch_session_item(
                    user_id,
                    session_id,
                    [
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
            "AND IS_DEFINED(c.summaryVersion) AND c.summaryVersion < @version"
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
                await self._patch_session_item(
                    user_id,
                    session_id,
                    [
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
        await self._patch_session_item(
            user_id,
            session_id,
            [{"op": "set", "path": "/updatedAt", "value": updated_at}],
        )

    async def delete_session(self, user_id: str, session_id: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        await self._owned_session(user_id, session_id)
        # Delete the parent FIRST -- this is the fence that closes a
        # concurrent-orphan race. Every child-writer (add_message,
        # add_message_if_summary_version, add_document, upsert_message)
        # re-verifies the parent still exists immediately after its own
        # write and self-compensates (deletes what it just wrote) if not.
        # So a child write that races with this delete is always caught by
        # ONE of two sides: if the child's write finishes before this
        # delete, its post-write recheck runs after the delete and catches
        # it directly; if the child's write finishes after this delete, the
        # children-sweep below (which runs only once the parent is
        # irrevocably gone) catches it. Deleting children before the parent
        # (the previous order) left a window where a child could land
        # strictly between the sweep's query snapshot and the parent
        # delete, which neither side would ever catch -- a permanent
        # orphan. Residual: if the sweep below hits a genuine non-404 error
        # after the parent is gone, a retry of this call can no longer
        # resume (there's nothing left to re-authenticate against); that
        # narrow, rare exposure is accepted in exchange for closing the
        # much more common orphan-creation race.
        try:
            await self._sessions.delete_item(item=session_id, partition_key=user_id)
        except CosmosResourceNotFoundError as exc:
            # Deleted concurrently (e.g. a duplicate request) — same outcome
            # as never having existed for this call.
            raise SessionNotFoundError(session_id) from exc

        query = "SELECT c.id FROM c WHERE c.sessionId = @sid"
        params = [{"name": "@sid", "value": session_id}]
        async for doc in self._messages.query_items(
            query=query, parameters=params, partition_key=session_id
        ):
            try:
                await self._messages.delete_item(item=doc["id"], partition_key=session_id)
            except CosmosResourceNotFoundError:
                # Another concurrent delete/cascade (e.g. a duplicate request,
                # a racing single-message delete, or a child-writer's own
                # compensating delete) already removed this child between the
                # query above and this call. The cascade's goal -- no
                # messages left for this session -- already holds for this
                # item, so treat it as done and keep going rather than
                # surfacing a raw SDK error for a row that's already gone.
                continue
        # Cascade-delete uploaded documents (also partitioned by sessionId).
        async for doc in self._documents.query_items(
            query=query, parameters=params, partition_key=session_id
        ):
            try:
                await self._documents.delete_item(item=doc["id"], partition_key=session_id)
            except CosmosResourceNotFoundError:
                continue  # same idempotent reasoning as the messages loop above

    async def add_message(self, user_id: str, message: Message) -> Message:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        await self._owned_session(user_id, message.sessionId)
        message.userId = user_id
        await self._messages.create_item(self._to_doc(message))
        # The parent can be deleted concurrently between the ownership check
        # above and this write landing: delete_session's children-sweep only
        # catches writes it can already see at query time. Re-verify the
        # parent still exists and self-compensate (delete what was just
        # written) if not, so a concurrent session delete can never leave a
        # message orphaned behind it.
        try:
            await self._owned_session(user_id, message.sessionId)
        except SessionNotFoundError:
            try:
                await self._messages.delete_item(item=message.id, partition_key=message.sessionId)
            except CosmosResourceNotFoundError:
                pass  # already swept by the concurrent delete_session
            raise
        return message

    async def add_message_if_summary_version(
        self, user_id: str, message: Message, *, expected_version: int
    ) -> bool:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        session = await self._owned_session(user_id, message.sessionId)
        if session.summaryVersion != expected_version:
            return False
        message.userId = user_id
        message.summaryVersion = expected_version
        await self._messages.create_item(self._to_doc(message))
        try:
            latest = await self._owned_session(user_id, message.sessionId)
        except SessionNotFoundError:
            # The parent was deleted concurrently after this write landed;
            # self-compensate so no message survives its session (same
            # reasoning as add_message's post-write recheck above).
            try:
                await self._messages.delete_item(
                    item=message.id, partition_key=message.sessionId
                )
            except CosmosResourceNotFoundError:
                pass
            raise
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
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        await self._owned_session(user_id, message.sessionId)
        message.userId = user_id
        await self._messages.upsert_item(self._to_doc(message))
        # Same post-write recheck-and-compensate as add_message: if the
        # session no longer exists, the message can't belong to it either
        # (whether this call created or updated it), so remove it rather
        # than leave it orphaned.
        try:
            await self._owned_session(user_id, message.sessionId)
        except SessionNotFoundError:
            try:
                await self._messages.delete_item(item=message.id, partition_key=message.sessionId)
            except CosmosResourceNotFoundError:
                pass
            raise
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
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        await self._owned_session(user_id, session_id)
        query = "SELECT c.id FROM c WHERE c.sessionId = @sid"
        params = [{"name": "@sid", "value": session_id}]
        async for doc in self._messages.query_items(
            query=query, parameters=params, partition_key=session_id
        ):
            try:
                await self._messages.delete_item(item=doc["id"], partition_key=session_id)
            except CosmosResourceNotFoundError:
                continue  # already gone (concurrent clear/delete) -- idempotent

    async def add_document(self, user_id: str, document: Document) -> Document:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        await self._owned_session(user_id, document.sessionId)
        document.userId = user_id
        await self._documents.create_item(self._to_doc(document))
        # Same post-write recheck-and-compensate as add_message: catch a
        # parent delete that raced between the ownership check and this
        # write landing.
        try:
            await self._owned_session(user_id, document.sessionId)
        except SessionNotFoundError:
            try:
                await self._documents.delete_item(
                    item=document.id, partition_key=document.sessionId
                )
            except CosmosResourceNotFoundError:
                pass  # already swept by the concurrent delete_session
            raise
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
