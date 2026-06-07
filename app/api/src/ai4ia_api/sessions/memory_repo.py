"""In-memory SessionRepository for local dev and tests.

Enforces the same ownership rules as the Cosmos implementation so behavior is
identical across stores.
"""
from __future__ import annotations

import asyncio

from .models import Message, Session
from .repository import SessionNotFoundError


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._messages: dict[str, list[Message]] = {}
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

    async def delete_session(self, user_id: str, session_id: str) -> None:
        async with self._lock:
            await self._owned_session(user_id, session_id)
            self._sessions.pop(session_id, None)
            self._messages.pop(session_id, None)

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
        return list(self._messages.get(session_id, []))
