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
        """Cascade-delete a session's messages and documents, then the session.

        RESIDUAL GAP (not fixed here): this method's own cascade is
        idempotent against *itself* (every delete below tolerates a row
        already being gone), but it is not a fence against a concurrent
        writer. A ``add_message``/``add_document`` call that passes the
        ownership check in another request between this method's cascade
        query and its own write can still land afterwards, orphaning that
        child forever. Earlier revisions of this method attempted to close
        that race with a CAS "deletingAt" tombstone plus a bounded sweep-loop
        and a delayed hard-delete; that machinery was reverted because a
        single Cosmos read is not a durable cross-replica consistency
        barrier, so the tombstone could not actually guarantee no in-flight
        write is missed -- it just made the failure mode harder to reason
        about while still not closing the race. Closing this properly needs
        either a real distributed transaction (children + parent in one
        atomic unit) or a background reconciliation/change-feed job that
        finds and removes orphaned children after the fact; neither exists
        today. Tracked as a known architectural limitation, not a bug to
        chase with another timing-based mitigation.
        """
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        await self._owned_session(user_id, session_id)
        # Delete child messages first (partition = sessionId).
        query = "SELECT c.id FROM c WHERE c.sessionId = @sid"
        params = [{"name": "@sid", "value": session_id}]
        async for doc in self._messages.query_items(
            query=query, parameters=params, partition_key=session_id
        ):
            try:
                await self._messages.delete_item(item=doc["id"], partition_key=session_id)
            except CosmosResourceNotFoundError:
                # Another concurrent delete/cascade (e.g. a duplicate request,
                # or a racing single-message delete) already removed this
                # child between the query above and this call. The cascade's
                # goal -- no messages left for this session -- already holds
                # for this item, so treat it as done and keep going rather
                # than surfacing a raw SDK error for a row that's already gone.
                continue
        # Cascade-delete uploaded documents (also partitioned by sessionId).
        async for doc in self._documents.query_items(
            query=query, parameters=params, partition_key=session_id
        ):
            try:
                await self._documents.delete_item(item=doc["id"], partition_key=session_id)
            except CosmosResourceNotFoundError:
                continue  # same idempotent reasoning as the messages loop above

        try:
            await self._sessions.delete_item(item=session_id, partition_key=user_id)
        except CosmosResourceNotFoundError as exc:
            # Deleted concurrently during the cascade above (e.g. a duplicate
            # request) — same outcome as never having existed for this call.
            raise SessionNotFoundError(session_id) from exc

    async def add_message(self, user_id: str, message: Message) -> Message:
        await self._owned_session(user_id, message.sessionId)
        message.userId = user_id
        await self._messages.create_item(self._to_doc(message))
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
        await self._owned_session(user_id, message.sessionId)
        message.userId = user_id
        await self._messages.upsert_item(self._to_doc(message))
        return message

    async def consume_tool_approval(
        self, user_id: str, session_id: str, message_id: str, request_id: str
    ) -> bool:
        """Flip one pending tool approval to spent with an ETag-conditional write.

        An unconditional read-modify-write is not good enough here. The approval
        is a one-shot capability to make a real outbound call, and two concurrent
        ``POST /api/chat`` requests presenting the same ``{requestId, grant}``
        would both read ``consumed=False`` from their own snapshot and both
        redeem it — doubling every side effect a single human decision
        authorized. The conditional replace makes exactly one of them win.

        Retries a bounded number of times because a *different* concurrent write
        to the same message (the terminal assistant upsert, for instance) also
        invalidates the ETag without meaning the approval was spent. On re-read
        the record is examined again, so a genuine loser sees ``consumed=True``
        and returns False rather than retrying into a second redemption.
        Mirrors the existing CAS loops in ``set_generated_title_if_eligible`` and
        ``mutate_library_document_ids``.
        """
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceNotFoundError,
        )

        await self._owned_session(user_id, session_id)
        for _attempt in range(3):
            try:
                raw = await self._messages.read_item(
                    item=message_id, partition_key=session_id
                )
            except CosmosResourceNotFoundError:
                return False
            approvals = raw.get("pendingApprovals") or []
            index = next(
                (
                    position
                    for position, record in enumerate(approvals)
                    if isinstance(record, dict) and record.get("id") == request_id
                ),
                None,
            )
            if index is None:
                return False
            if approvals[index].get("consumed") is True:
                return False
            try:
                await self._messages.patch_item(
                    item=message_id,
                    partition_key=session_id,
                    patch_operations=[
                        {
                            "op": "set",
                            "path": f"/pendingApprovals/{index}/consumed",
                            "value": True,
                        }
                    ],
                    etag=raw.get("_etag"),
                    match_condition=MatchConditions.IfNotModified,
                )
                return True
            except CosmosAccessConditionFailedError:
                continue
        # Bounded retries exhausted under sustained contention: deny. An
        # approval we cannot prove we spent must not authorize a call.
        return False

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
