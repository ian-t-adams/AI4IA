"""Persist complete, independently bounded workflow-step execution receipts."""
from __future__ import annotations

from typing import Any

from ..agents.consent import ToolConsentSummary
from ..agents.tools import redact
from ..model_evidence import ReceiptCostSummary, combine_costs
from ..receipts import (
    MAX_DELEGATIONS, MAX_TOOLS_OFFERED, ExecutionReceipt, ReceiptRuntime, build_receipt,
    enforce_receipt_budget,
)
from ..safety import MessageSafety, merge_safety
from ..sessions.models import ActivityStep
from .runner import WorkflowRunResult, WorkflowStepResult
from ..publishing.models import exact_digest


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
    include_steps: bool = False,
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
    if receipt.runtime.publication is not None:
        evidence = receipt.runtime.publication
        effective = [
            child.runtime.publication.effectiveSubsetDigest for child in children
            if child.runtime.publication is not None
        ]
        receipt.runtime.publication = evidence.model_copy(update={
            "scope": "run",
            "effectiveSubsetDigest": exact_digest(effective) if effective and all(effective) else None,
            "narrowing": tuple(sorted({
                reason for child in children if child.runtime.publication is not None
                for reason in child.runtime.publication.narrowing
            })),
        })
    receipt.toolCallCount = sum(child.toolCallCount for child in children)
    receipt.autoApprovedToolCalls = sum(child.autoApprovedToolCalls for child in children)
    receipt.usage.cost = combine_costs(
        [child.usage.cost or ReceiptCostSummary(totalCalls=child.usage.calls) for child in children],
        expected_calls=result.usage.calls,
    )
    offers = [offer for child in children for offer in child.toolsOffered]
    receipt.toolsOffered = [offer.model_copy(deep=True) for offer in offers[:MAX_TOOLS_OFFERED]]
    receipt.toolsOfferedCount = sum(child.toolsOfferedCount for child in children)
    receipt.notes.append("workflow_step_receipts")
    if include_steps:
        receipt.delegations = [child.model_copy(deep=True) for child in children[:MAX_DELEGATIONS]]
        if len(children) > MAX_DELEGATIONS:
            receipt.notes.append("delegations_capped")
            receipt.truncated = True
    if any(child.truncated for child in children):
        receipt.truncated = True
    return enforce_receipt_budget(receipt)
