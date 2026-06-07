"""Cosmos DB (NoSQL) SessionRepository using AAD (managed identity) auth.

Containers (created by infra/modules/data.bicep):
- ``sessions``  PK ``/userId``
- ``messages``  PK ``/sessionId`` (userId denormalized + ownership-checked)

Azure SDKs are imported lazily so the app and tests run without them installed.
"""
from __future__ import annotations

from typing import Any

from .models import Message, Session
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

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()

    @staticmethod
    def _to_doc(model: Session | Message) -> dict[str, Any]:
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

    async def delete_session(self, user_id: str, session_id: str) -> None:
        await self._owned_session(user_id, session_id)
        # Delete child messages first (partition = sessionId).
        query = "SELECT c.id FROM c WHERE c.sessionId = @sid"
        params = [{"name": "@sid", "value": session_id}]
        async for doc in self._messages.query_items(query=query, parameters=params):
            await self._messages.delete_item(item=doc["id"], partition_key=session_id)
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
