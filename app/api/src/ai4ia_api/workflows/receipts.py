"""Persist complete, independently bounded workflow-step execution receipts."""
from __future__ import annotations

from typing import Any

from ..agents.consent import ToolConsentSummary
from ..agents.tools import redact
from ..receipts import (
    MAX_TOOLS_OFFERED, ExecutionReceipt, ReceiptRuntime, build_receipt,
    enforce_receipt_budget,
)
from ..safety import MessageSafety, merge_safety
from ..sessions.models import ActivityStep
from .runner import WorkflowRunResult, WorkflowStepResult


def step_to_dict(step: WorkflowStepResult) -> dict[str, Any]:
    return {
        "agent": step.agent, "ok": step.ok, "text": step.text, "error": step.error,
        "iterations": step.iterations, "cancelled": step.cancelled,
        "receipt": step.receipt.model_dump(mode="json") if step.receipt else None,
        "activity": [item.model_dump(mode="json") for item in step.activity],
        "safety": step.safety.model_dump(mode="json") if step.safety else None,
    }


def step_from_dict(value: dict[str, Any]) -> WorkflowStepResult:
    return WorkflowStepResult(
        agent=value.get("agent") or "unknown", ok=value.get("ok", False),
        text=value.get("text") or "", error=value.get("error"),
        iterations=value.get("iterations", 0), cancelled=value.get("cancelled", False),
        receipt=ExecutionReceipt.model_validate(value["receipt"]) if value.get("receipt") else None,
        activity=[ActivityStep.model_validate(item) for item in value.get("activity") or []],
        safety=MessageSafety.model_validate(value["safety"]) if value.get("safety") else None,
    )


def workflow_safety(steps: list[WorkflowStepResult]) -> MessageSafety | None:
    summary = None
    for step in steps:
        summary = merge_safety(summary, step.safety)
    return summary


def workflow_activity(steps: list[WorkflowStepResult]) -> list[ActivityStep]:
    activity: list[ActivityStep] = []
    for index, step in enumerate(steps):
        activity.append(ActivityStep(
            kind="workflow_step" if step.ok else "workflow_error",
            label=f"Step {index + 1}: {' '.join(redact(step.agent).split())[:64]}",
            detail="completed" if step.ok else ("cancelled" if step.cancelled else "failed"),
        ))
        activity.extend(step.activity)
    return activity


def workflow_receipt(
    result: WorkflowRunResult, *, runtime: ReceiptRuntime,
    correlation_id: str | None = None, consent: ToolConsentSummary | None = None,
) -> ExecutionReceipt:
    children = [step.receipt for step in result.steps if step.receipt is not None]
    receipt = build_receipt(
        correlation_id=correlation_id, runtime=runtime,
        calls=[call for child in children for call in child.toolCalls],
        approvals_requested=sum(child.approvalsRequested for child in children),
        approvals_granted=sum(child.approvalsGranted for child in children),
        tool_consent=consent, usage=result.usage, safety=workflow_safety(result.steps),
        iterations=sum(step.iterations for step in result.steps),
        status="cancelled" if result.cancelled else ("complete" if result.ok else "error"),
        partial=not result.ok,
    )
    receipt.toolCallCount = sum(child.toolCallCount for child in children)
    receipt.autoApprovedToolCalls = sum(child.autoApprovedToolCalls for child in children)
    offers = [offer for child in children for offer in child.toolsOffered]
    receipt.toolsOffered = offers[:MAX_TOOLS_OFFERED]
    receipt.toolsOfferedCount = sum(child.toolsOfferedCount for child in children)
    receipt.notes.append("workflow_step_receipts")
    if any(child.truncated for child in children):
        receipt.truncated = True
    return enforce_receipt_budget(receipt)
