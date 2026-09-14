"""The run checkpoint and its public message form one fenced transition."""
from __future__ import annotations

from ..sessions.deletion_models import DeletionIntegrityError, DeletionMigrationRequiredError
from ..sessions.models import Message, MessageRole, MessageStatus, Session
from .automation_models import TERMINAL_STATES, WorkflowCheckpoint


def validate_pair(
    session: Session, checkpoint: WorkflowCheckpoint, assistant: Message, *,
    user_message: Message | None = None,
) -> None:
    if session.deletionProtocol != 1:
        raise DeletionMigrationRequiredError()
    if (
        checkpoint.userId != session.userId or checkpoint.sessionId != session.id
        or checkpoint.deletionEpoch != session.deletionEpoch
        or assistant.userId != session.userId or assistant.sessionId != session.id
        or assistant.workflowRunId != checkpoint.runId
        or assistant.workflowRunFingerprint != checkpoint.fingerprint
        or assistant.workflowRunStatus != checkpoint.status
        or assistant.role is not MessageRole.assistant
        or checkpoint.id.startswith("__ai4ia_") or assistant.id.startswith("__ai4ia_")
        or checkpoint.id == assistant.id
    ):
        raise DeletionIntegrityError("Workflow checkpoint/message binding mismatch")
    if user_message is not None and (
        user_message.userId != session.userId or user_message.sessionId != session.id
        or user_message.role is not MessageRole.user
        or user_message.workflowRunId != checkpoint.runId
        or user_message.workflowRunFingerprint != checkpoint.fingerprint
        or user_message.id in {checkpoint.id, assistant.id}
        or user_message.id.startswith("__ai4ia_")
    ):
        raise DeletionIntegrityError("Workflow start message binding mismatch")


def validate_transition(
    prior: WorkflowCheckpoint, updated: WorkflowCheckpoint,
    prior_message: Message, updated_message: Message,
) -> None:
    if (
        updated.id != prior.id or updated.runId != prior.runId
        or updated.userId != prior.userId or updated.sessionId != prior.sessionId
        or updated.fingerprint != prior.fingerprint
        or updated.deletionEpoch != prior.deletionEpoch
        or updated.ownerEpoch != prior.ownerEpoch
        or updated.revision != prior.revision + 1
        or updated.createdAt != prior.createdAt or updated.deadline != prior.deadline
        or updated.limits != prior.limits
        or (not prior.allowTools and updated.allowTools)
        or (not prior.allowAutomaticMemory and updated.allowAutomaticMemory)
        or updated.bundle != prior.bundle or updated.input != prior.input
        or updated.scheduleId != prior.scheduleId
        or updated.scheduleGeneration != prior.scheduleGeneration
        or updated.step < prior.step
        or len(updated.completedSteps) < len(prior.completedSteps)
        or updated.completedSteps[:len(prior.completedSteps)] != prior.completedSteps
        or (prior.status in TERMINAL_STATES and updated.status != prior.status)
        or (prior_message.workflowConsentRevoked and not updated_message.workflowConsentRevoked)
        or (
            prior_message.status is not MessageStatus.streaming
            and updated_message.status is MessageStatus.streaming
        )
    ):
        raise DeletionIntegrityError("Workflow transition cannot restore or change authority")


def cleared_checkpoint(prior: WorkflowCheckpoint) -> WorkflowCheckpoint:
    return prior.model_copy(update={
        "status": "cancelled", "reason": "conversation_cleared", "revision": prior.revision + 1,
        "wakeRevision": prior.wakeRevision + 1, "leaseId": None, "leaseExpiresAt": None, "bundle": None,
        "input": "", "previous": "", "turn": None, "completedSteps": [],
        "currentResult": None, "currentUsage": None,
        "memoryContext": None,
        "draft": None, "approvalHistory": [],
    }, deep=True)
