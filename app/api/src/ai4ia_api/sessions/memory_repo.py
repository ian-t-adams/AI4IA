"""In-memory SessionRepository for local dev and tests.

Enforces the same ownership rules as the Cosmos implementation so behavior is
identical across stores.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .models import Document, Message, Session
from .repository import SessionNotFoundError


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._messages: dict[str, list[Message]] = {}
        self._documents: dict[str, list[Document]] = {}
        self._lock = asyncio.Lock()

    async def _owned_session(self, user_id: str, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None or session.userId != user_id:
            raise SessionNotFoundError(session_id)
        return session

    async def create_session(self, session: Session) -> Session:
        async with self._lock:
            self._sessions[session.id] = session
            self._messages.setdefault(session.id, [])
            return session

    async def get_session(self, user_id: str, session_id: str) -> Session:
        return await self._owned_session(user_id, session_id)

    async def list_sessions(self, user_id: str) -> list[Session]:
        items = [s for s in self._sessions.values() if s.userId == user_id]
        return sorted(items, key=lambda s: s.updatedAt, reverse=True)

    async def update_session(self, session: Session) -> Session:
        async with self._lock:
            await self._owned_session(session.userId, session.id)
            self._sessions[session.id] = session
            return session

    async def touch_session(self, user_id: str, session_id: str) -> None:
        async with self._lock:
            session = await self._owned_session(user_id, session_id)
            session.updatedAt = datetime.now(timezone.utc)

    async def delete_session(self, user_id: str, session_id: str) -> None:
        async with self._lock:
            await self._owned_session(user_id, session_id)
            self._sessions.pop(session_id, None)
            self._messages.pop(session_id, None)
            self._documents.pop(session_id, None)

    async def add_message(self, user_id: str, message: Message) -> Message:
        async with self._lock:
            await self._owned_session(user_id, message.sessionId)
            message.userId = user_id
            self._messages.setdefault(message.sessionId, []).append(message)
            return message

    async def upsert_message(self, user_id: str, message: Message) -> Message:
        async with self._lock:
            await self._owned_session(user_id, message.sessionId)
            message.userId = user_id
            bucket = self._messages.setdefault(message.sessionId, [])
            for idx, existing in enumerate(bucket):
                if existing.id == message.id:
                    bucket[idx] = message
                    return message
            bucket.append(message)
            return message

    async def list_messages(self, user_id: str, session_id: str) -> list[Message]:
        await self._owned_session(user_id, session_id)
        return sorted(
            self._messages.get(session_id, []),
            key=lambda message: (message.createdAt, message.id),
        )

    async def clear_messages(self, user_id: str, session_id: str) -> None:
        async with self._lock:
            await self._owned_session(user_id, session_id)
            self._messages[session_id] = []

    async def add_document(self, user_id: str, document: Document) -> Document:
        async with self._lock:
            await self._owned_session(user_id, document.sessionId)
            document.userId = user_id
            self._documents.setdefault(document.sessionId, []).append(document)
            return document

    async def list_documents(self, user_id: str, session_id: str) -> list[Document]:
        await self._owned_session(user_id, session_id)
        docs = list(self._documents.get(session_id, []))
        return sorted(docs, key=lambda d: d.createdAt)

    async def get_document(
        self, user_id: str, session_id: str, document_id: str
    ) -> Document | None:
        await self._owned_session(user_id, session_id)
        for doc in self._documents.get(session_id, []):
            if doc.id == document_id:
                return doc
        return None

    async def delete_document(
        self, user_id: str, session_id: str, document_id: str
    ) -> None:
        async with self._lock:
            await self._owned_session(user_id, session_id)
            bucket = self._documents.get(session_id, [])
            self._documents[session_id] = [d for d in bucket if d.id != document_id]
