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

from .models import Document, Message, Session
from .repository import SessionNotFoundError


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
        await self._owned_session(session.userId, session.id)
        await self._sessions.upsert_item(self._to_doc(session))
        return session

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

    async def upsert_message(self, user_id: str, message: Message) -> Message:
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
