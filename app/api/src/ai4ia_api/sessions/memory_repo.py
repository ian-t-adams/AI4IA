"""In-memory SessionRepository for local dev and tests.

Enforces the same ownership rules as the Cosmos implementation so behavior is
identical across stores.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

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
        normalized = normalize_session_patch_changes(changes)
        async with self._lock:
            session = await self._owned_session(user_id, session_id)
            for field_name, value in normalized.items():
                setattr(session, field_name, value)
            session.updatedAt = datetime.now(timezone.utc)
            return session.model_copy(deep=True)

    async def set_generated_title_if_eligible(
        self, user_id: str, session_id: str, title: str
    ) -> bool:
        normalized = normalize_session_title(title)
        async with self._lock:
            session = await self._owned_session(user_id, session_id)
            if session.title != "New chat" or session.titleSource == "manual":
                return False
            session.title = normalized
            session.titleSource = "auto"
            session.updatedAt = datetime.now(timezone.utc)
            return True

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
            self._messages[session_id] = [
                message
                for message in self._messages.get(session_id, [])
                if message.clientTurnId is not None
                or message.summaryVersion is None
                or message.summaryVersion >= session.summaryVersion
            ]
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
            self._messages[session_id] = [
                message
                for message in self._messages.get(session_id, [])
                if message.clientTurnId is not None
                or message.summaryVersion is None
                or message.summaryVersion >= session.summaryVersion
            ]
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

    async def claim_chat_turn(
        self, user_id: str, user_message: Message, assistant_message: Message
    ) -> tuple[Message, Message, bool]:
        async with self._lock:
            await self._owned_session(user_id, user_message.sessionId)
            if (
                user_message.sessionId != assistant_message.sessionId
                or not user_message.clientTurnId
                or user_message.clientTurnId != assistant_message.clientTurnId
            ):
                raise ValueError("A chat turn must have one session and clientTurnId.")
            bucket = self._messages.setdefault(user_message.sessionId, [])
            existing = [
                message
                for message in bucket
                if message.clientTurnId == user_message.clientTurnId
            ]
            if existing:
                by_role = {message.role: message for message in existing}
                saved_user = by_role.get(user_message.role)
                saved_assistant = by_role.get(assistant_message.role)
                if (
                    saved_user is None
                    or saved_assistant is None
                    or saved_user.clientRequestFingerprint
                    != user_message.clientRequestFingerprint
                    or saved_assistant.clientRequestFingerprint
                    != assistant_message.clientRequestFingerprint
                ):
                    raise ClientTurnConflictError(user_message.clientTurnId)
                return (
                    saved_user.model_copy(deep=True),
                    saved_assistant.model_copy(deep=True),
                    False,
                )
            user_message.userId = user_id
            assistant_message.userId = user_id
            bucket.extend(
                (
                    user_message.model_copy(deep=True),
                    assistant_message.model_copy(deep=True),
                )
            )
            return (
                user_message.model_copy(deep=True),
                assistant_message.model_copy(deep=True),
                True,
            )

    async def get_chat_turn(
        self,
        user_id: str,
        session_id: str,
        client_turn_id: str,
        fingerprint: str,
    ) -> tuple[Message, Message] | None:
        async with self._lock:
            await self._owned_session(user_id, session_id)
            bucket = self._messages.get(session_id, [])
            ids = {
                role: turn_message_id(user_id, session_id, client_turn_id, role)
                for role in (MessageRole.user, MessageRole.assistant)
            }
            found = {
                message.role: message
                for message in bucket
                if message.id in ids.values()
            }
            if not found:
                return None
            saved_user = found.get(MessageRole.user)
            saved_assistant = found.get(MessageRole.assistant)
            if (
                saved_user is None
                or saved_assistant is None
                or saved_user.userId != user_id
                or saved_assistant.userId != user_id
                or saved_user.clientTurnId != client_turn_id
                or saved_assistant.clientTurnId != client_turn_id
                or saved_user.clientRequestFingerprint != fingerprint
                or saved_assistant.clientRequestFingerprint != fingerprint
            ):
                raise ClientTurnConflictError(client_turn_id)
            return (
                saved_user.model_copy(deep=True),
                saved_assistant.model_copy(deep=True),
            )

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
        if expected_claim_lease_id is None and stale_before is None:
            raise ValueError("A claim lease or stale cutoff is required.")
        async with self._lock:
            await self._owned_session(user_id, session_id)
            for message in self._messages.get(session_id, []):
                if message.id != assistant_message_id:
                    continue
                if message.userId != user_id:
                    raise SessionNotFoundError(session_id)
                if message.status.value != "streaming":
                    return message.model_copy(deep=True)
                if (
                    expected_claim_lease_id is not None
                    and message.claimLeaseId != expected_claim_lease_id
                ):
                    return message.model_copy(deep=True)
                if stale_before is not None and message.createdAt > stale_before:
                    return message.model_copy(deep=True)
                message.status = status
                message.content = content
                message.steps = steps
                message.attachments = list(attachments or [])
                message.summaryVersion = summary_version
                return message.model_copy(deep=True)
            return None

    async def add_message_if_summary_version(
        self, user_id: str, message: Message, *, expected_version: int
    ) -> bool:
        if message.claimLeaseId is not None:
            raise ValueError("Claimed turns must use terminalize_chat_turn.")
        async with self._lock:
            session = await self._owned_session(user_id, message.sessionId)
            if session.summaryVersion != expected_version:
                return False
            message.userId = user_id
            message.summaryVersion = expected_version
            bucket = self._messages.setdefault(message.sessionId, [])
            for index, existing in enumerate(bucket):
                if existing.id == message.id:
                    bucket[index] = message
                    break
            else:
                bucket.append(message)
            return True

    async def upsert_message(self, user_id: str, message: Message) -> Message:
        if message.claimLeaseId is not None:
            raise ValueError("Claimed turns must use terminalize_chat_turn.")
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
            [
                message.model_copy(deep=True)
                for message in self._messages.get(session_id, [])
            ],
            key=lambda message: message.createdAt,
        )

    async def clear_messages(self, user_id: str, session_id: str) -> None:
        async with self._lock:
            await self._owned_session(user_id, session_id)
            self._messages[session_id] = []

    async def clear_messages_before(
        self,
        user_id: str,
        session_id: str,
        *,
        cutoff: datetime,
        preserve_ids: frozenset[str],
    ) -> None:
        async with self._lock:
            await self._owned_session(user_id, session_id)
            streaming_turn_ids = {
                message.clientTurnId
                for message in self._messages.get(session_id, [])
                if message.status is MessageStatus.streaming
                and message.clientTurnId is not None
            }
            self._messages[session_id] = [
                message
                for message in self._messages.get(session_id, [])
                if message.id in preserve_ids
                or message.clientTurnId in streaming_turn_ids
                or message.createdAt > cutoff
            ]

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
