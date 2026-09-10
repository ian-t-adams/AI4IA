"""SessionRepository protocol + shared errors.

Every method takes the authenticated ``user_id`` and MUST enforce ownership:
sessions are partitioned by user; messages are partitioned by session, so each
message operation first proves the parent session belongs to the user.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..agents.consent import ToolConsentState
from .deletion_models import (
    DeletionLease,
    DeletionPage,
    DeletionStatus,
    InitializationPage,
    UploadIntent,
)
from .models import Document, Message, Session


class SessionNotFoundError(Exception):
    """Raised when a session does not exist or is not owned by the user."""


class SessionConflictError(Exception):
    """Raised when a bounded session CAS mutation cannot be committed."""


@runtime_checkable
class SessionRepository(Protocol):
    async def list_initializations(self, user_id: str, cursor: str = "") -> InitializationPage: ...

    async def check_deletion_ready(self) -> None: ...

    async def begin_deletion(self, user_id: str, session_id: str) -> DeletionStatus: ...

    async def get_deletion_status(self, user_id: str, session_id: str) -> DeletionStatus: ...

    async def list_deletions(self, user_id: str, cursor: str = "") -> DeletionPage: ...

    async def claim_deletion(self, user_id: str, session_id: str) -> DeletionLease | None: ...

    async def checkpoint_deletion(
        self, lease: DeletionLease, status: DeletionStatus, *, release: bool
    ) -> DeletionLease: ...

    async def close_deletion_fences(self, lease: DeletionLease) -> None: ...

    async def cleanup_child_page(
        self, lease: DeletionLease, *, documents: bool
    ) -> bool: ...

    async def deletion_uploads(
        self, lease: DeletionLease, *, unsettled_only: bool
    ) -> list[UploadIntent]: ...

    async def remove_settled_uploads(
        self, lease: DeletionLease, intents: list[UploadIntent]
    ) -> None: ...

    async def reserve_attachment_upload(
        self, user_id: str, session_id: str, document_id: str, *, storage_id: str
    ) -> UploadIntent | None: ...

    async def settle_attachment_upload(self, intent: UploadIntent) -> None: ...

    async def check_ready(self) -> None:
        """Prove the backing store is reachable without reading user data."""
        ...

    async def create_session(self, session: Session) -> Session: ...

    async def get_session(self, user_id: str, session_id: str) -> Session: ...

    async def claim_fresh_session(self, user_id: str, expected: Session) -> Session | None:
        """Consume one v1 session's fresh-turn slot on the exact owner/snapshot.

        Only a successful atomic claim may build the constrained prompt. The
        marker never expires or resets, including after failure or /clear.
        This is not a fence against child mutations; v1 deletion owns those.
        """
        ...

    async def list_sessions(self, user_id: str) -> list[Session]: ...

    async def patch_session(
        self, user_id: str, session_id: str, changes: dict[str, object]
    ) -> Session: ...

    async def set_tool_consent(
        self, user_id: str, session_id: str, consent: ToolConsentState | None,
        *, expected_version: int | None = None,
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

    async def claim_workflow_run_if_absent(
        self,
        user_id: str,
        user_message: Message,
        pending_assistant: Message,
    ) -> bool:
        """Atomically create both deterministic workflow claim messages.

        Both rows share the session partition. Returns False when either id
        exists; implementations must create both rows or neither row.
        """
        ...

    async def replace_message_if_workflow_status(
        self,
        user_id: str,
        message: Message,
        *,
        expected_status: str,
        expected_lease_token: str | None,
        expected_message: Message | None = None,
    ) -> bool:
        """Replace only while status, lease and any caller snapshot still match.

        A snapshot binds receipt-bearing writes to the content the caller read,
        not merely a status shared by successive checkpoints.
        """
        ...

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
