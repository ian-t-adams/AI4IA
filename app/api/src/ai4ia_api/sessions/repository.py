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

    async def set_generated_title_if_eligible(
        self, user_id: str, session_id: str, title: str
    ) -> bool: ...

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

    async def consume_tool_approval(
        self, user_id: str, session_id: str, message_id: str, request_id: str
    ) -> bool:
        """Atomically mark one pending tool approval as spent.

        Returns True only for the caller that actually flipped it from unspent to
        spent; every loser (already spent, missing message, missing record, or a
        lost race) gets False and must deny. This MUST be a single
        compare-and-set, not a read-then-write: the approval is a one-shot
        capability to make a real outbound call, so two concurrent requests
        presenting the same grant both seeing ``consumed=False`` would both
        redeem it. See :mod:`ai4ia_api.agents.approvals`.
        """
        ...

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
