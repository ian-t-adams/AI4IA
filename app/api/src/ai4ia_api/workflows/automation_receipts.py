"""Project canonical continuation evidence without exposing executable state."""
from __future__ import annotations

import json

from ..agents.activity import persisted_trace
from ..agents.receipt import ReceiptDraft
from ..agents.runtime import AgentStep
from ..agents.turn_checkpoint import CheckpointRecorder
from ..model_evidence import ModelCallRecorder
from ..receipts import ExecutionReceipt, ReceiptRuntime, enforce_receipt_budget
from ..sessions.models import ActivityStep, Message, MessageRole, MessageStatus
from ..usage.models import TokenUsage
from .automation_models import TERMINAL_STATES, WorkflowCheckpoint
from .durable import durable_message_ids
from .receipts import workflow_activity, workflow_receipt
from .runner import WorkflowRunResult, WorkflowStepResult


def active_step(state: WorkflowCheckpoint) -> tuple[WorkflowStepResult | None, TokenUsage]:
    if state.currentResult is not None:
        return state.currentResult, state.currentUsage or TokenUsage.empty()
    turn, bundle = state.turn, state.bundle
    if turn is None or bundle is None or state.step >= len(bundle.workflow.steps):
        return None, TokenUsage.empty()
    steps = [AgentStep(**item) for item in turn.steps]
    draft = state.draft
    if draft is not None and state.status in {"denied", "expired"}:
        steps.append(AgentStep(
            kind="tool_denied", tool=draft.tool, arguments=json.loads(draft.argumentsJson),
            detail="approval_" + state.status,
        ))
    usage = turn.usage.model_copy(deep=True)
    if turn.phase == "model" and state.operationState in {"dispatched", "unknown"}:
        usage = usage.add(TokenUsage.parse(None))
    requests = sum(item.step == state.step for item in state.approvalHistory) + int(draft is not None)
    granted = sum(
        item.step == state.step and item.state in {"approved", "dispatched"}
        for item in state.approvalHistory
    ) + int(draft is not None and draft.state in {"approved", "dispatched"})
    name = bundle.workflow.steps[state.step].agent
    receipt = ReceiptDraft(
        runtime=ReceiptRuntime(
            modelId=bundle.modelId, deployment=bundle.deployment.deploymentName,
            api=bundle.api, agent=name,
        ),
        model_evidence=CheckpointRecorder(ModelCallRecorder(), turn),
        approvals_granted=granted,
    ).build(
        steps=steps, iterations=turn.iterations, status="incomplete", partial=True,
        approvals_requested=requests, offered=turn.offeredTools,
        prompt_messages=turn.effectivePrompt, model_requests=turn.modelRequests,
        usage=usage, safety=turn.safety,
    )
    return WorkflowStepResult(
        agent=name, ok=False, text="".join(turn.completedText), iterations=turn.iterations,
        receipt=receipt, activity=persisted_trace(steps), safety=turn.safety,
    ), usage


def project_message(state: WorkflowCheckpoint, previous: Message | None = None) -> Message:
    bundle = state.bundle
    finished = [step.result for step in state.completedSteps]
    usage = TokenUsage.empty()
    for step in state.completedSteps:
        usage = usage.add(step.usage)
    current, current_usage = active_step(state)
    evidence_steps = [*finished, *([current] if current else [])]
    usage = usage.add(current_usage)
    cancelled = state.status == "cancelled"
    ok = state.status == "completed"
    content = state.previous if ok else (
        "Workflow is waiting for your approval." if state.status == "awaiting_approval"
        else "Current interactive authorization is required." if state.status == "reauthentication_required"
        else "Workflow is running." if state.status not in TERMINAL_STATES
        else f"Workflow stopped: {state.reason or state.status}. Already dispatched work may have completed."
    )
    runtime = ReceiptRuntime(
        modelId=bundle.modelId if bundle else None,
        deployment=bundle.deployment.deploymentName if bundle else None,
        api=bundle.api if bundle else None,
        agent=f"workflow:{bundle.workflow.name}" if bundle else None,
        workflowConfigSha256=state.fingerprint,
    )
    receipt = workflow_receipt(
        WorkflowRunResult(ok=ok, text=content, steps=evidence_steps, usage=usage, cancelled=cancelled),
        runtime=runtime,
    ) if bundle else ExecutionReceipt(runtime=runtime, status="cancelled", partial=True)
    if state.status not in TERMINAL_STATES:
        receipt.status = "incomplete"
        receipt.partial = True
    receipt.notes.append("durable_exact_call_v3")
    activity = workflow_activity(finished)
    if current is not None:
        activity.extend(current.activity)
    if state.status in {"awaiting_approval", "reauthentication_required"}:
        activity.append(ActivityStep(
            kind="workflow_wait", label=f"Step {state.step + 1}: waiting",
            detail=state.status,
        ))
    _, message_id = durable_message_ids(state.runId)
    message = Message(
        id=message_id, userId=state.userId, sessionId=state.sessionId,
        role=MessageRole.assistant, content=content,
        status=(
            MessageStatus.complete if ok else MessageStatus.cancelled if cancelled
            else MessageStatus.error if state.status in TERMINAL_STATES else MessageStatus.streaming
        ),
        model=bundle.deployment.deploymentName if bundle else None,
        agent=f"workflow:{bundle.workflow.name}" if bundle else None,
        workflowRunId=state.runId, workflowRunStatus=state.status,
        workflowRunFingerprint=state.fingerprint,
        workflowConsentRevoked=state.status in TERMINAL_STATES and not ok,
        workflowStepReceipts=[step.receipt for step in evidence_steps if step.receipt],
        steps=activity, executionReceipt=enforce_receipt_budget(receipt),
        createdAt=previous.createdAt if previous else state.createdAt,
    )
    if previous is not None:
        message.workflowScheduleLeaseToken = previous.workflowScheduleLeaseToken
        message.workflowScheduleLeaseExpiresAt = previous.workflowScheduleLeaseExpiresAt
    return message
