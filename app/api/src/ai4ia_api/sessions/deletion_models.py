"""Retained deletion evidence, never a certificate of physical erasure."""
from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
from datetime import datetime, timezone
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1
FENCE_ID = "__ai4ia_session_fence_v1__"
UPLOAD_ID_PREFIX = "__ai4ia_upload_v1__:"
CONTROL_PARTITION = "__ai4ia_deletion_control__"
CAS_ATTEMPTS = 3
CLEANUP_ITEMS = 25
STATUS_ITEMS = 50
LEASE_SECONDS = 60


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class DeletionUnavailableError(Exception):
    """The authoritative deletion/write state could not be established."""


class DeletionMigrationRequiredError(Exception):
    """An unversioned conversation requires separately approved enrollment."""


class DeletionDisabledError(Exception):
    """New deletion work is disabled; retained v1 guards still apply."""


class DeletionIntegrityError(Exception):
    """Unexpected owner, generation, discriminator or coordination state."""


class InitializationCursorError(ValueError):
    """Malformed opaque owner-list cursor."""


def initialization_cursor(item_id: str) -> str:
    return base64.urlsafe_b64encode(item_id.encode("ascii")).decode("ascii").rstrip("=")


def initialization_after(cursor: str) -> str:
    if not cursor:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,172}", cursor):
        raise InitializationCursorError()
    try:
        value = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        ).decode("ascii")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise InitializationCursorError() from exc
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
        or initialization_cursor(value) != cursor
    ):
        raise InitializationCursorError()
    return value


class SessionInitialization(BaseModel):
    sessionId: str
    createdAt: datetime
    state: Literal["initializing"] = "initializing"


class InitializationPage(BaseModel):
    items: list[SessionInitialization]
    hasMore: bool = False
    nextCursor: str | None = None
    observation: Literal["not_completion_evidence"] = "not_completion_evidence"


class InitializationRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    userId: str
    kind: Literal["session_initializing_v1"]
    deletionProtocol: Literal[1] = 1
    deletionEpoch: str = Field(min_length=1)
    createdAt: datetime
    attachmentStorageRequired: bool = Field(default=False, strict=True)
    attachmentStorageId: str | None = None
    ttl: Literal[-1] = -1


class UploadIntent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    sessionId: str
    userId: str
    epoch: str
    kind: Literal["session_upload_v1"] = "session_upload_v1"
    documentId: str
    startedAt: datetime = Field(default_factory=now_utc)
    settled: bool = Field(default=False, strict=True)
    ttl: Literal[-1] = -1


class PendingUpload(BaseModel):
    id: str
    documentId: str
    startedAt: datetime


class DeletionStatus(BaseModel):
    sessionId: str
    state: Literal["pending", "retryable", "cleanup_verified"] = "pending"
    phase: Literal[
        "fences", "messages", "documents", "attachments", "uploads", "complete"
    ] = "fences"
    requestedAt: datetime = Field(default_factory=now_utc)
    updatedAt: datetime = Field(default_factory=now_utc)
    lastVerifiedAt: datetime | None = None
    messagesVerified: bool = False
    documentsVerified: bool = False
    attachmentsVerified: bool = False
    pendingUploads: list[PendingUpload] = Field(default_factory=list, max_length=CLEANUP_ITEMS)
    pendingUploadsTruncated: bool = False
    retryReason: Literal[
        "storage_unavailable", "cleanup_timeout", "concurrent_change",
        "integrity_mismatch", "uploads_unresolved", "artifact_store_required",
    ] | None = None
    attempts: int = Field(default=0, ge=0)
    # The API names both the evidence's scope and the retained exceptions.
    scope: Literal["conversation_content_and_inline_originals"] = (
        "conversation_content_and_inline_originals"
    )
    backupsErased: Literal[False] = False
    coordinationRetained: Literal[True] = True
    autonomousCleanup: Literal[False] = False


class DeletionRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    userId: str
    kind: Literal["session_tombstone_v1"] = "session_tombstone_v1"
    deletionProtocol: Literal[1] = 1
    deletionEpoch: str
    # Persist the requirement, not the current feature toggle: disabling inline
    # compute later must not turn a missing Blob account into verified cleanup.
    attachmentStorageRequired: bool = Field(default=False, strict=True)
    attachmentStorageId: str | None = None
    status: DeletionStatus
    leaseToken: str | None = None
    leaseExpiresAt: datetime | None = None
    ttl: Literal[-1] = -1


@dataclass(frozen=True)
class DeletionLease:
    record: DeletionRecord
    etag: str
    token: str


class DeletionPage(BaseModel):
    items: list[DeletionStatus]
    hasMore: bool = False
    nextCursor: str | None = None


class RolloutApproval(BaseModel):
    """Only an operator may author this record after reviewed cutover evidence."""
    model_config = ConfigDict(extra="ignore")

    id: str
    userId: Literal["__ai4ia_deletion_control__"]
    kind: Literal["session_deletion_rollout_v1"]
    protocol: Literal[1]
    state: Literal["approved"]
    scope: Literal["new_sessions_only"]
    singleWriteRegion: Literal[True]
    noCoordinationExpiry: Literal[True]
    writerCutoverEvidence: str = Field(min_length=1, max_length=1000)
    recoveryReviewEvidence: str = Field(min_length=1, max_length=1000)
