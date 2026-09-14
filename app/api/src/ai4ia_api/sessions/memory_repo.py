"""In-memory SessionRepository for local dev and tests.

Enforces the same ownership rules as the Cosmos implementation so behavior is
identical across stores.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..agents.consent import ToolConsentState
from .deletion_models import (
    CLEANUP_ITEMS,
    LEASE_SECONDS,
    STATUS_ITEMS,
    UPLOAD_ID_PREFIX,
    DeletionDisabledError,
    DeletionIntegrityError,
    DeletionLease,
    DeletionMigrationRequiredError,
    DeletionPage,
    DeletionRecord,
    DeletionStatus,
    DeletionUnavailableError,
    InitializationPage,
    UploadIntent,
    initialization_after,
    now_utc,
)
from .models import (
    Document,
    Message,
    Session,
    normalize_session_patch_changes,
    normalize_session_title,
)
from .repository import SessionConflictError, SessionNotFoundError


class InMemorySessionRepository:
    def __init__(
        self, *, deletion_enabled: bool = False, attachment_storage_required: bool = False,
        attachment_storage_id: str | None = None,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._messages: dict[str, list[Message]] = {}
        self._documents: dict[str, list[Document]] = {}
        self._lock = asyncio.Lock()
        self._deletion_enabled = deletion_enabled
        self._attachment_storage_required = attachment_storage_required
        self._attachment_storage_id = attachment_storage_id
        self._deletions: dict[tuple[str, str], tuple[DeletionRecord, int]] = {}
        self._uploads: dict[str, UploadIntent] = {}

    async def check_ready(self) -> None:
        return None

    async def _owned_session(self, user_id: str, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None or session.userId != user_id:
            raise SessionNotFoundError(session_id)
        return session

    async def create_session(self, session: Session) -> Session:
        async with self._lock:
            if any(sid == session.id for _, sid in self._deletions):
                raise SessionConflictError(session.id)
            if self._deletion_enabled:
                if session.id in self._sessions:
                    raise SessionConflictError(session.id)
                session.deletionProtocol = 1
                session.deletionEpoch = str(uuid4())
                session.attachmentStorageRequired = self._attachment_storage_required
                session.attachmentStorageId = (
                    self._attachment_storage_id if self._attachment_storage_required else None
                )
            elif session.deletionProtocol is not None:
                raise DeletionDisabledError()
            self._sessions[session.id] = session
            self._messages.setdefault(session.id, [])
            return session

    async def get_session(self, user_id: str, session_id: str) -> Session:
        return (await self._owned_session(user_id, session_id)).model_copy(deep=True)

    async def claim_fresh_session(self, user_id: str, expected: Session) -> Session | None:
        async with self._lock:
            current = await self._owned_session(user_id, expected.id)
            if (
                not self._deletion_enabled or current.deletionProtocol != 1
                or current.freshTurnClaimed or current != expected
            ):
                return None
            current.freshTurnClaimed = True
            return current.model_copy(deep=True)

    async def list_sessions(self, user_id: str) -> list[Session]:
        items = [
            s.model_copy(deep=True)
            for s in self._sessions.values()
            if s.userId == user_id
        ]
        return sorted(items, key=lambda s: s.updatedAt, reverse=True)

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

    async def set_tool_consent(
        self, user_id: str, session_id: str, consent: ToolConsentState | None,
        *, expected_version: int | None = None,
    ) -> Session:
        if consent is not None and (
            consent.userId != user_id or consent.sessionId != session_id
            or consent.grant.scope != "session" or consent.runId is not None
        ):
            raise ValueError("Consent must belong to this session.")
        async with self._lock:
            session = await self._owned_session(user_id, session_id)
            if expected_version is not None and session.toolConsentVersion != expected_version:
                raise SessionConflictError(session_id)
            session.toolConsentState = consent.model_copy(deep=True) if consent else None
            session.toolConsent = consent.grant if consent else None
            session.toolConsentVersion += 1
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
                if message.summaryVersion is None
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
                if message.summaryVersion is None
                or message.summaryVersion >= session.summaryVersion
            ]
            return session.model_copy(deep=True)

    async def touch_session(self, user_id: str, session_id: str) -> None:
        async with self._lock:
            session = await self._owned_session(user_id, session_id)
            session.updatedAt = datetime.now(timezone.utc)

    async def delete_session(self, user_id: str, session_id: str) -> None:
        async with self._lock:
            session = await self._owned_session(user_id, session_id)
            if session.deletionProtocol == 1:
                raise DeletionDisabledError()
            if self._deletion_enabled:
                raise DeletionMigrationRequiredError()
            self._sessions.pop(session_id, None)
            self._messages.pop(session_id, None)
            self._documents.pop(session_id, None)

    async def add_message(self, user_id: str, message: Message) -> Message:
        async with self._lock:
            await self._owned_session(user_id, message.sessionId)
            message.userId = user_id
            self._messages.setdefault(message.sessionId, []).append(message)
            return message

    async def add_message_if_summary_version(
        self, user_id: str, message: Message, *, expected_version: int
    ) -> bool:
        async with self._lock:
            session = await self._owned_session(user_id, message.sessionId)
            if session.summaryVersion != expected_version:
                return False
            message.userId = user_id
            message.summaryVersion = expected_version
            self._messages.setdefault(message.sessionId, []).append(message)
            return True

    async def claim_workflow_run_if_absent(
        self,
        user_id: str,
        user_message: Message,
        pending_assistant: Message,
    ) -> bool:
        if user_message.sessionId != pending_assistant.sessionId:
            raise ValueError("workflow claim messages must share one session")
        async with self._lock:
            await self._owned_session(user_id, user_message.sessionId)
            bucket = self._messages.setdefault(user_message.sessionId, [])
            claimed_ids = {user_message.id, pending_assistant.id}
            if any(existing.id in claimed_ids for existing in bucket):
                return False
            user_message.userId = user_id
            pending_assistant.userId = user_id
            bucket.extend(
                [
                    user_message.model_copy(deep=True),
                    pending_assistant.model_copy(deep=True),
                ]
            )
            return True

    async def replace_message_if_workflow_status(
        self,
        user_id: str,
        message: Message,
        *,
        expected_status: str,
        expected_lease_token: str | None,
        expected_message: Message | None = None,
    ) -> bool:
        async with self._lock:
            await self._owned_session(user_id, message.sessionId)
            bucket = self._messages.setdefault(message.sessionId, [])
            for idx, existing in enumerate(bucket):
                if existing.id != message.id:
                    continue
                if (
                    existing.workflowRunStatus != expected_status
                    or existing.workflowRunFingerprint
                    != message.workflowRunFingerprint
                    or existing.workflowScheduleLeaseToken
                    != expected_lease_token
                    or (expected_message is not None and existing != expected_message)
                ):
                    return False
                message.userId = user_id
                bucket[idx] = message.model_copy(deep=True)
                return True
            return False

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

    async def consume_tool_approval(
        self, user_id: str, session_id: str, message_id: str, request_id: str
    ) -> bool:
        """Flip one pending tool approval to spent, atomically.

        Held under the repository lock so the check and the write cannot
        interleave — the in-memory analogue of the Cosmos ETag CAS. Getting this
        right here matters even though this store is dev/test-only: it is the
        store the approval tests run against, so a non-atomic version would make
        those tests unable to observe the very race they exist to rule out.
        """
        async with self._lock:
            await self._owned_session(user_id, session_id)
            for message in self._messages.get(session_id, []):
                if message.id != message_id:
                    continue
                for record in message.pendingApprovals or []:
                    if record.id != request_id:
                        continue
                    if record.consumed:
                        return False
                    record.consumed = True
                    return True
            return False

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

    async def check_deletion_ready(self) -> None:
        # Local-only store. Distributed safety is exercised by transactional
        # Cosmos fakes, not inferred from this process-local lock.
        return None

    async def list_initializations(self, user_id: str, cursor: str = "") -> InitializationPage:
        initialization_after(cursor)
        # Local creation is atomic under one lock and has no external awaits;
        # distributed incomplete initialization is covered by the Cosmos fake.
        return InitializationPage(items=[])

    def _owned_deletion(self, user_id: str, session_id: str) -> tuple[DeletionRecord, int]:
        value = self._deletions.get((user_id, session_id))
        if value is None:
            raise SessionNotFoundError(session_id)
        return value

    async def begin_deletion(self, user_id: str, session_id: str) -> DeletionStatus:
        if not self._deletion_enabled:
            raise DeletionDisabledError()
        async with self._lock:
            existing = self._deletions.get((user_id, session_id))
            if existing is not None:
                return existing[0].status.model_copy(deep=True)
            session = await self._owned_session(user_id, session_id)
            if session.deletionProtocol != 1:
                raise DeletionMigrationRequiredError()
            record = DeletionRecord(
                id=session_id, userId=user_id, deletionEpoch=session.deletionEpoch or "",
                attachmentStorageRequired=session.attachmentStorageRequired,
                attachmentStorageId=session.attachmentStorageId,
                status=DeletionStatus(sessionId=session_id),
            )
            self._deletions[(user_id, session_id)] = (record, 1)
            del self._sessions[session_id]
            return record.status.model_copy(deep=True)

    async def get_deletion_status(self, user_id: str, session_id: str) -> DeletionStatus:
        return self._owned_deletion(user_id, session_id)[0].status.model_copy(deep=True)

    async def list_deletions(self, user_id: str, cursor: str = "") -> DeletionPage:
        records = sorted(
            (
                record for (uid, sid), (record, _) in self._deletions.items()
                if uid == user_id and sid > cursor
            ),
            key=lambda record: record.id,
        )[:STATUS_ITEMS + 1]
        more = len(records) > STATUS_ITEMS
        return DeletionPage(
            items=[record.status.model_copy(deep=True) for record in records[:STATUS_ITEMS]],
            hasMore=more, nextCursor=records[STATUS_ITEMS - 1].id if more else None,
        )

    async def claim_deletion(self, user_id: str, session_id: str) -> DeletionLease | None:
        if not self._deletion_enabled:
            raise DeletionDisabledError()
        async with self._lock:
            stored, version = self._owned_deletion(user_id, session_id)
            now = now_utc()
            if stored.status.state == "cleanup_verified" or (
                stored.leaseToken and stored.leaseExpiresAt and stored.leaseExpiresAt > now
            ):
                return None
            record = stored.model_copy(deep=True)
            token = str(uuid4())
            record.leaseToken = token
            record.leaseExpiresAt = now + timedelta(seconds=LEASE_SECONDS)
            record.status.attempts += 1
            record.status.state = "pending"
            record.status.retryReason = None
            record.status.updatedAt = now
            self._deletions[(user_id, session_id)] = (record, version + 1)
            return DeletionLease(
                record=record.model_copy(deep=True), etag=str(version + 1), token=token
            )

    async def checkpoint_deletion(
        self, lease: DeletionLease, status: DeletionStatus, *, release: bool
    ) -> DeletionLease:
        async with self._lock:
            current, version = self._owned_deletion(lease.record.userId, lease.record.id)
            if str(version) != lease.etag or current.leaseToken != lease.token:
                raise DeletionUnavailableError("Deletion lease changed")
            record = current.model_copy(deep=True)
            record.status = status.model_copy(deep=True)
            record.status.updatedAt = now_utc()
            if release:
                record.leaseToken = None
                record.leaseExpiresAt = None
            self._deletions[(record.userId, record.id)] = (record, version + 1)
            return DeletionLease(
                record=record.model_copy(deep=True), etag=str(version + 1), token=lease.token
            )

    async def close_deletion_fences(self, lease: DeletionLease) -> None:
        self._owned_deletion(lease.record.userId, lease.record.id)

    async def cleanup_child_page(
        self, lease: DeletionLease, *, documents: bool
    ) -> bool:
        async with self._lock:
            self._owned_deletion(lease.record.userId, lease.record.id)
            if documents:
                rows = self._documents.get(lease.record.id, [])[:CLEANUP_ITEMS]
            else:
                rows = self._messages.get(lease.record.id, [])[:CLEANUP_ITEMS]
            if any(
                row.userId != lease.record.userId or row.sessionId != lease.record.id
                or row.id.startswith("__ai4ia_") for row in rows
            ):
                raise DeletionIntegrityError("Unexpected conversation child")
            if documents:
                self._documents[lease.record.id] = self._documents.get(
                    lease.record.id, []
                )[CLEANUP_ITEMS:]
            else:
                self._messages[lease.record.id] = self._messages.get(
                    lease.record.id, []
                )[CLEANUP_ITEMS:]
            return not rows

    async def deletion_uploads(
        self, lease: DeletionLease, *, unsettled_only: bool
    ) -> list[UploadIntent]:
        self._owned_deletion(lease.record.userId, lease.record.id)
        rows = [
            intent.model_copy(deep=True) for intent in self._uploads.values()
            if intent.sessionId == lease.record.id
            and (not unsettled_only or not intent.settled)
        ][:CLEANUP_ITEMS + 1]
        if any(
            intent.userId != lease.record.userId or intent.epoch != lease.record.deletionEpoch
            for intent in rows
        ):
            raise DeletionIntegrityError("Upload intent does not match")
        return rows

    async def remove_settled_uploads(
        self, lease: DeletionLease, intents: list[UploadIntent]
    ) -> None:
        async with self._lock:
            self._owned_deletion(lease.record.userId, lease.record.id)
            for intent in intents[:CLEANUP_ITEMS]:
                if (
                    not intent.settled or intent.userId != lease.record.userId
                    or intent.sessionId != lease.record.id
                    or intent.epoch != lease.record.deletionEpoch
                ):
                    raise DeletionIntegrityError("Unsettled upload cannot be forgotten")
                self._uploads.pop(intent.id, None)

    async def reserve_attachment_upload(
        self, user_id: str, session_id: str, document_id: str, *, storage_id: str
    ) -> UploadIntent | None:
        async with self._lock:
            session = await self._owned_session(user_id, session_id)
            if session.deletionProtocol != 1:
                return None
            if session.attachmentStorageId not in (None, storage_id):
                raise DeletionIntegrityError("Conversation attachment store changed")
            session.attachmentStorageRequired = True
            session.attachmentStorageId = storage_id
            intent = UploadIntent(
                id=UPLOAD_ID_PREFIX + str(uuid4()), sessionId=session_id, userId=user_id,
                epoch=session.deletionEpoch or "", documentId=document_id,
            )
            self._uploads[intent.id] = intent.model_copy(deep=True)
            return intent

    async def settle_attachment_upload(self, intent: UploadIntent) -> None:
        async with self._lock:
            current = self._uploads.get(intent.id)
            if current is None or current.model_copy(update={"settled": False}) != intent.model_copy(
                update={"settled": False}
            ):
                raise DeletionIntegrityError("Upload completion does not match its intent")
            current.settled = True
