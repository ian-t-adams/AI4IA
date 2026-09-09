"""Owner-requested, bounded reconciliation. There is no autonomous purge job."""
from __future__ import annotations

import asyncio
import logging

from pydantic import ValidationError

from ..documents.ephemeral_store import EphemeralAttachmentStore
from .deletion_models import (
    CLEANUP_ITEMS,
    DeletionIntegrityError,
    DeletionStatus,
    DeletionUnavailableError,
    PendingUpload,
    now_utc,
)
from .repository import SessionRepository

logger = logging.getLogger(__name__)
CLEANUP_TIMEOUT_SECONDS = 20


class ConversationDeletionService:
    def __init__(
        self, repo: SessionRepository, artifacts: EphemeralAttachmentStore | None
    ) -> None:
        self._repo = repo
        self._artifacts = artifacts

    async def reconcile(self, user_id: str, session_id: str) -> DeletionStatus:
        from azure.core.exceptions import AzureError

        lease = None
        progress = None
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                lease = await self._repo.claim_deletion(user_id, session_id)
                if lease is None:
                    return await self._repo.get_deletion_status(user_id, session_id)
                progress = lease.record.status.model_copy(deep=True)
                await self._repo.close_deletion_fences(lease)
                progress.phase = "messages"
                lease = await self._repo.checkpoint_deletion(lease, progress, release=False)
                progress.messagesVerified = await self._repo.cleanup_child_page(
                    lease, documents=False
                )
                progress.phase = "documents"
                lease = await self._repo.checkpoint_deletion(lease, progress, release=False)
                progress.documentsVerified = await self._repo.cleanup_child_page(
                    lease, documents=True
                )
                progress.phase = "attachments"
                lease = await self._repo.checkpoint_deletion(lease, progress, release=False)
                unresolved = await self._repo.deletion_uploads(lease, unsettled_only=True)
                progress.pendingUploads = [
                    PendingUpload(id=item.id, documentId=item.documentId, startedAt=item.startedAt)
                    for item in unresolved[:CLEANUP_ITEMS]
                ]
                progress.pendingUploadsTruncated = len(unresolved) > CLEANUP_ITEMS
                if lease.record.attachmentStorageRequired and (
                    self._artifacts is None
                    or lease.record.attachmentStorageId != self._artifacts.storage_id
                ):
                    progress.state = "retryable"
                    progress.retryReason = "artifact_store_required"
                    progress.attachmentsVerified = False
                else:
                    empty = (
                        await self._artifacts.reconcile_session(
                            user_id, session_id, limit=CLEANUP_ITEMS
                        )
                        if lease.record.attachmentStorageRequired and self._artifacts else True
                    )
                    # Even an empty Blob scan is not proof against a paused PUT.
                    progress.attachmentsVerified = empty and not unresolved
                    if unresolved:
                        progress.phase = "uploads"
                        progress.retryReason = "uploads_unresolved"
                    elif empty:
                        settled = await self._repo.deletion_uploads(lease, unsettled_only=False)
                        await self._repo.remove_settled_uploads(lease, settled)
                        if (
                            not settled and progress.messagesVerified
                            and progress.documentsVerified
                        ):
                            progress.state = "cleanup_verified"
                            progress.phase = "complete"
                            progress.lastVerifiedAt = now_utc()
                        elif settled:
                            progress.phase = "uploads"
                if progress.state == "pending" and not progress.retryReason:
                    if not progress.messagesVerified:
                        progress.phase = "messages"
                    elif not progress.documentsVerified:
                        progress.phase = "documents"
                lease = await self._repo.checkpoint_deletion(lease, progress, release=True)
                return lease.record.status
        except TimeoutError:
            reason = "cleanup_timeout"
        except (DeletionIntegrityError, ValidationError):
            reason = "integrity_mismatch"
        except DeletionUnavailableError:
            reason = "concurrent_change"
        except AzureError:
            reason = "storage_unavailable"
        if lease is None or progress is None:
            raise DeletionUnavailableError("Cleanup ownership could not be established")
        progress.retryReason = reason
        progress.state = "retryable"
        progress.attachmentsVerified = False
        logger.warning(
            "conversation cleanup retryable session=%s reason=%s",
            session_id, progress.retryReason,
        )
        # If ownership was lost, this CAS raises unavailable rather than making
        # an old worker's status overwrite the new owner's progress.
        try:
            async with asyncio.timeout(5):
                saved = await self._repo.checkpoint_deletion(lease, progress, release=True)
        except TimeoutError as exc:
            raise DeletionUnavailableError("Cleanup failure could not be checkpointed") from exc
        return saved.record.status
