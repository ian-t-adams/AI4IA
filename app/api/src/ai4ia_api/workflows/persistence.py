"""CAS updates keep cancellation monotonic while late work retains its receipt."""
from __future__ import annotations

from ..receipts import enforce_receipt_budget
from ..sessions.models import Message, MessageStatus
from ..sessions.repository import SessionConflictError, SessionRepository


async def persist_run_message(
    repo: SessionRepository, user_id: str, message: Message,
) -> Message:
    for _attempt in range(3):
        messages = await repo.list_messages(user_id, message.sessionId)
        current = next((item for item in messages if item.id == message.id), None)
        if current is None or current.workflowRunFingerprint != message.workflowRunFingerprint:
            raise SessionConflictError(message.sessionId)
        expected = current.model_copy(deep=True)
        if (
            current.status is not MessageStatus.streaming
            and message.status is MessageStatus.streaming
            and not current.workflowConsentRevoked
        ):
            return current
        updated = message.model_copy(deep=True)
        updated.createdAt = current.createdAt
        updated.workflowToolConsent = current.workflowToolConsent
        updated.workflowToolConsentState = current.workflowToolConsentState
        if len(current.workflowStepReceipts or []) > len(updated.workflowStepReceipts or []):
            retained = current.model_copy(deep=True)
            updated.workflowStepReceipts = retained.workflowStepReceipts
            updated.steps = retained.steps
            updated.executionReceipt = retained.executionReceipt
        if current.workflowConsentRevoked:
            updated.workflowConsentRevoked = True
            updated.status = MessageStatus.cancelled
            updated.workflowRunStatus = "cancelled"
            if not updated.content:
                updated.content = "Workflow cancelled. Already in-flight work may have completed."
            if updated.executionReceipt is not None:
                updated.executionReceipt.status = "cancelled"
                updated.executionReceipt.partial = True
                updated.executionReceipt = enforce_receipt_budget(updated.executionReceipt)
        if await repo.replace_message_if_workflow_status(
            user_id, updated, expected_status=current.workflowRunStatus or "pending",
            expected_lease_token=current.workflowScheduleLeaseToken,
            expected_message=expected,
        ):
            return updated
    raise SessionConflictError(message.sessionId)
