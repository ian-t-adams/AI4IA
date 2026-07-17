"""In-memory SessionRepository for local dev and tests.

Enforces the same ownership rules as the Cosmos implementation so behavior is
identical across stores.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .models import Document, Message, Session
from .repository import SessionConflictError, SessionNotFoundError


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
        return (await self._owned_session(user_id, session_id)).model_copy(deep=True)

    async def list_sessions(self, user_id: str) -> list[Session]:
        items = [
            s.model_copy(deep=True)
            for s in self._sessions.values()
            if s.userId == user_id
        ]
        return sorted(items, key=lambda s: s.updatedAt, reverse=True)

    async def update_session(self, session: Session) -> Session:
        raise SessionConflictError(
            "Unversioned full session replacement is disabled; use patch_session."
        )

    async def patch_session(
        self, user_id: str, session_id: str, changes: dict[str, object]
    ) -> Session:
        async with self._lock:
            session = await self._owned_session(user_id, session_id)
            for field_name, value in changes.items():
                setattr(session, field_name, value)
            session.updatedAt = datetime.now(timezone.utc)
            return session.model_copy(deep=True)

    async def mutate_library_document_ids(
        self,
        user_id: str,
        session_id: str,
        document_id: str,
        *,
        add: bool,
        legacy_ids: list[str] | None = None,
    ) -> Session:
        async with self._lock:
            session = await self._owned_session(user_id, session_id)
            current = session.libraryDocumentIds
            if current is None:
                if add:
                    return session.model_copy(deep=True)
                current = list(legacy_ids or [])
            else:
                current = list(current)
            if add and document_id not in current:
                current.append(document_id)
            elif not add:
                current = [value for value in current if value != document_id]
            session.libraryDocumentIds = current
            session.updatedAt = datetime.now(timezone.utc)
            return session.model_copy(deep=True)

    async def invalidate_summary(
        self, user_id: str, session_id: str
    ) -> Session:
        async with self._lock:
            session = await self._owned_session(user_id, session_id)
            session.summary = None
            session.summarizedThroughMessageId = None
            session.summaryVersion += 1
            session.updatedAt = datetime.now(timezone.utc)
            return session.model_copy(deep=True)

    async def commit_summary_if_version(
        self,
        user_id: str,
        session_id: str,
        *,
        expected_version: int,
        summary: str,
        summarized_through_message_id: str,
    ) -> Session | None:
        async with self._lock:
            session = await self._owned_session(user_id, session_id)
            if session.summaryVersion != expected_version:
                return None
            session.summary = summary
            session.summarizedThroughMessageId = summarized_through_message_id
            session.summaryVersion = expected_version + 1
            session.updatedAt = datetime.now(timezone.utc)
            return session.model_copy(deep=True)

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
            key=lambda message: message.createdAt,
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
