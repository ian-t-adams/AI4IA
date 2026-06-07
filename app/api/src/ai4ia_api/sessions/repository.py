"""SessionRepository protocol + shared errors.

Every method takes the authenticated ``user_id`` and MUST enforce ownership:
sessions are partitioned by user; messages are partitioned by session, so each
message operation first proves the parent session belongs to the user.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Message, Session


class SessionNotFoundError(Exception):
    """Raised when a session does not exist or is not owned by the user."""


@runtime_checkable
class SessionRepository(Protocol):
    async def create_session(self, session: Session) -> Session: ...

    async def get_session(self, user_id: str, session_id: str) -> Session: ...

    async def list_sessions(self, user_id: str) -> list[Session]: ...

    async def update_session(self, session: Session) -> Session: ...

    async def delete_session(self, user_id: str, session_id: str) -> None: ...

    async def add_message(self, user_id: str, message: Message) -> Message: ...

    async def upsert_message(self, user_id: str, message: Message) -> Message: ...

    async def list_messages(self, user_id: str, session_id: str) -> list[Message]: ...
