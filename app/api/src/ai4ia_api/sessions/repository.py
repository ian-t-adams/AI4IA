"""SessionRepository protocol + shared errors.

Every method takes the authenticated ``user_id`` and MUST enforce ownership:
sessions are partitioned by user; messages are partitioned by session, so each
message operation first proves the parent session belongs to the user.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Document, Message, Session


class SessionNotFoundError(Exception):
    """Raised when a session does not exist or is not owned by the user."""


class SessionConflictError(Exception):
    """Raised when a bounded session CAS mutation cannot be committed."""


@runtime_checkable
class SessionRepository(Protocol):
    async def create_session(self, session: Session) -> Session: ...

    async def get_session(self, user_id: str, session_id: str) -> Session: ...

    async def list_sessions(self, user_id: str) -> list[Session]: ...

    async def update_session(self, session: Session) -> Session: ...

    async def patch_session(
        self, user_id: str, session_id: str, changes: dict[str, object]
    ) -> Session: ...

    async def mutate_library_document_ids(
        self,
        user_id: str,
        session_id: str,
        document_id: str,
        *,
        add: bool,
        legacy_ids: list[str] | None = None,
    ) -> Session: ...

    async def invalidate_summary(
        self, user_id: str, session_id: str
    ) -> Session: ...

    async def commit_summary_if_version(
        self,
        user_id: str,
        session_id: str,
        *,
        expected_version: int,
        summary: str,
        summarized_through_message_id: str,
    ) -> Session | None: ...

    async def touch_session(self, user_id: str, session_id: str) -> None: ...

    async def delete_session(self, user_id: str, session_id: str) -> None: ...

    async def add_message(self, user_id: str, message: Message) -> Message: ...

    async def add_message_if_summary_version(
        self, user_id: str, message: Message, *, expected_version: int
    ) -> bool: ...

    async def upsert_message(self, user_id: str, message: Message) -> Message: ...

    async def list_messages(self, user_id: str, session_id: str) -> list[Message]: ...

    async def clear_messages(self, user_id: str, session_id: str) -> None: ...

    async def add_document(self, user_id: str, document: Document) -> Document: ...

    async def list_documents(self, user_id: str, session_id: str) -> list[Document]: ...

    async def get_document(
        self, user_id: str, session_id: str, document_id: str
    ) -> Document | None: ...

    async def delete_document(
        self, user_id: str, session_id: str, document_id: str
    ) -> None: ...
