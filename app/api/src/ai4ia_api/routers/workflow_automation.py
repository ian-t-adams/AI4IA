"""Explicit owner-only automation APIs; no standing workflow approval endpoint."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Request
from pydantic import Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..sessions.repository import SessionNotFoundError
from ..sessions.models import Message
from ..request_constraints import constrain_request
from ..workflows.automation_access import WorkflowSelection
from ..workflows.automation_common import AutomationError, AutomationModel, ExecutionLimits, MAX_SCHEDULES
from ..workflows.automation_models import WorkflowCheckpoint, WorkflowSchedule
from ..workflows.automation_service import WorkflowAutomationService
from ..workflows.schedule_service import WorkflowScheduleService
from ..workflows.scheduling import ScheduleRule
from ..workflows.monetary_models import ApprovalSpendView, BudgetView
from ..workflows.monetary_quotes import run_account

router = APIRouter(prefix="/api/workflows/automation", tags=["workflow automation"])


class StartRequest(AutomationModel):
    selection: WorkflowSelection
    input: str = Field(min_length=1, max_length=8000)
    limits: ExecutionLimits
    idempotencyKey: str = Field(min_length=32, max_length=128)
    sessionId: str | None = Field(default=None, max_length=128)
    allowTools: bool = Field(default=True, strict=True)
    allowAutomaticMemory: bool = Field(default=True, strict=True)


class ScheduleRequest(AutomationModel):
    selection: WorkflowSelection
    input: str = Field(min_length=1, max_length=8000)
    limits: ExecutionLimits
    rule: ScheduleRule
    idempotencyKey: str = Field(min_length=32, max_length=128)
    expectedRevision: int | None = Field(default=None, ge=0, strict=True)
    allowTools: bool = Field(default=True, strict=True)
    allowAutomaticMemory: bool = Field(default=True, strict=True)


class RevisionRequest(AutomationModel):
    expectedRevision: int = Field(ge=0, strict=True)


class DecisionRequest(AutomationModel):
    decision: Literal["approve", "deny"]
    requestId: str | None = Field(default=None, max_length=128)
    grant: str | None = Field(default=None, max_length=256)


class ReviewRequest(AutomationModel):
    refreshSpendQuote: bool = Field(default=False, strict=True)


class ApprovalView(AutomationModel):
    id: str
    tool: str
    label: str
    risk: str
    purpose: str
    destination: str | None
    expiresAt: str
    state: str
    argumentsDigest: str
    spend: ApprovalSpendView


class RunView(AutomationModel):
    runId: str
    sessionId: str
    status: str
    reason: str | None
    revision: int = Field(ge=0, strict=True)
    workflow: str
    deadline: str | None
    step: int = Field(ge=0, strict=True)
    approval: ApprovalView | None
    budget: BudgetView
    message: Message | None = None


class ReviewView(RunView):
    argumentsJson: str
    requestId: str
    grant: str
    approvedDigest: str
    effectiveDigest: str
    spendImpact: str
    spendEvidence: ApprovalSpendView


def service(request: Request) -> WorkflowAutomationService:
    return request.app.state.workflow_automation


def run_view(state: WorkflowCheckpoint, budget: BudgetView) -> dict[str, Any]:
    draft = state.draft
    return RunView.model_validate({
        "runId": state.runId, "sessionId": state.sessionId, "status": state.status,
        "reason": state.reason, "revision": state.revision,
        "workflow": state.bundle.workflow.displayName if state.bundle else "Workflow",
        "deadline": state.deadline.isoformat(), "step": state.step,
        "approval": {
            "id": draft.id, "tool": draft.canonicalTool, "label": draft.label,
            "risk": draft.risk, "purpose": draft.purpose, "destination": draft.destination,
            "expiresAt": draft.expiresAt.isoformat(), "state": draft.state,
            "argumentsDigest": draft.argumentsDigest,
            "spend": ApprovalSpendView.from_quote(draft.spend),
        } if draft else None,
        "budget": budget,
    }).model_dump(mode="json", exclude_unset=True)


async def observed_run_view(current: WorkflowAutomationService, state: WorkflowCheckpoint) -> dict[str, Any]:
    return run_view(state, BudgetView.from_account(
        run_account((await current.owner(state.userId)).value, state),
    ))


def schedule_view(value: WorkflowSchedule) -> dict[str, Any]:
    return {
        "id": value.scheduleId, "revision": value.revision, "generation": value.generation,
        "enabled": value.enabled, "status": value.status, "reason": value.reason,
        "workflow": value.bundle.workflow.displayName, "workflowName": value.bundle.workflow.name,
        "source": value.bundle.source, "model": value.bundle.modelId,
        "input": value.input, "limits": value.limits.model_dump(mode="json"),
        "allowTools": value.allowTools, "allowAutomaticMemory": value.allowAutomaticMemory,
        "rule": value.rule.model_dump(mode="json"),
        "next": value.next.model_dump(mode="json") if value.next else None,
        "consumed": value.consumed, "tools": sorted(value.bundle.toolContracts),
        "history": [item.model_dump(mode="json") for item in value.history],
        "safeOnly": True, "approvedDigest": value.bundle.approvedBundleDigest,
        "effectiveDigest": value.bundle.bundleDigest,
    }


async def active_run_views(current: WorkflowAutomationService, owner: str) -> list[dict[str, Any]]:
    stored = await current.store.read_owner(owner)
    if stored is None:
        return []
    result = []
    for handle in stored.value.runs.values():
        if not handle.active:
            continue
        try:
            state, _ = await current.load(owner, handle.runId)
        except SessionNotFoundError:
            state = None
        except AutomationError as exc:
            if exc.code not in {"coordination_missing", "context_revoked"}:
                raise
            state = None
        result.append(run_view(state, BudgetView.from_account(
            run_account(stored.value, state),
        )) if state is not None else {
            "runId": handle.runId, "sessionId": handle.sessionId,
            "workflow": "Workflow preparation", "status": "preparation_unavailable",
            "reason": "The conversation or checkpoint is unavailable. Stop this preparation before starting again.",
            "revision": 0, "deadline": None, "step": 0, "approval": None,
            "budget": BudgetView.from_account(handle.money).model_dump(mode="json"),
        })
    return result[:20]


@router.get("/config")
async def config(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    current = service(request)
    settings = request.app.state.settings
    available = bool(
        settings.workflow_approvals_enabled and current.host is not None
        and request.app.state.usage.enabled and not settings.hard_quota_enabled
    )
    return {
        "approvalsAvailable": available,
        "schedulesAvailable": available and settings.workflow_scheduling_enabled,
        "maxSchedules": MAX_SCHEDULES, "maxRuntimeSeconds": settings.durable_workflow_timeout_seconds,
        "hardDollarCapAvailable": False, "spendMode": "no_hard_dollar_cap",
        "monetaryCapAvailable": False,
        "monetaryCapProfile": "stateless_text_only",
        "monetaryCapUnavailableReason": "verified_gateway_required",
    }


@router.post("/runs", status_code=202, response_model=RunView, response_model_exclude_unset=True)
async def start(
    request: Request, body: StartRequest, user: AuthenticatedUser = Depends(get_current_user),
):
    current = service(request)
    current.require_enabled()
    with constrain_request(tools=body.allowTools, automatic_memory=body.allowAutomaticMemory):
        bundle = await current.access.freeze(
            user, body.selection, body.limits,
            session_id=body.sessionId or "automation-admission", safe_only=False,
        )
        result = await current.start(
            bundle, body.input, body.limits, body.idempotencyKey,
            user=user, session_id=body.sessionId,
        )
    return await observed_run_view(current, result)


@router.get("/runs/{run_id}", response_model=RunView, response_model_exclude_unset=True)
async def read_run(request: Request, run_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    current = service(request)
    state, message = await current.load(user.internal_user_id, run_id)
    return {**await observed_run_view(current, state), "message": message.model_dump(mode="json")}


@router.get("/runs")
async def active_runs(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    return {"runs": await active_run_views(service(request), user.internal_user_id)}


@router.post("/runs/{run_id}/recover-start", status_code=202)
async def recover_run_start(request: Request, run_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    current = service(request)
    return await observed_run_view(current, await current.recover_start(user.internal_user_id, run_id))


@router.post("/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    current = service(request)
    state = await current.cancel(user.internal_user_id, run_id)
    return await observed_run_view(current, state) if state else {
        "runId": run_id, "status": "cancelled", "preparationOnly": True,
    }


@router.get("/approvals")
async def approvals(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    return {"runs": await active_run_views(service(request), user.internal_user_id)}


@router.post(
    "/runs/{run_id}/approvals/{draft_id}/review", response_model=ReviewView,
    response_model_exclude_unset=True,
)
async def review(
    request: Request, run_id: str, draft_id: str, body: ReviewRequest | None = Body(default=None),
    user: AuthenticatedUser = Depends(get_current_user),
):
    current = service(request)
    state, grant = await current.review(
        user.internal_user_id, run_id, draft_id, user,
        refresh_spend=body.refreshSpendQuote if body else False,
    )
    assert state.draft is not None and state.draft.challenge is not None
    return {
        **await observed_run_view(current, state), "argumentsJson": state.draft.argumentsJson,
        "requestId": state.draft.challenge.id, "grant": grant,
        "approvedDigest": state.bundle.approvedBundleDigest if state.bundle else None,
        "effectiveDigest": state.bundle.bundleDigest if state.bundle else None,
        "spendImpact": (
            "Spend impact applies only to this exact call, not later model work. "
            "Unsupported service charges remain unknown; this is not an Azure bill cap."
        ),
        "spendEvidence": ApprovalSpendView.from_quote(state.draft.spend).model_dump(mode="json"),
    }


@router.post("/runs/{run_id}/approvals/{draft_id}/decision", status_code=202)
async def decide(
    request: Request, run_id: str, draft_id: str, body: DecisionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    current = service(request)
    state = await current.decide(
        user.internal_user_id, run_id, draft_id, user=user, decision=body.decision,
        request_id=body.requestId, grant=body.grant,
    )
    return await observed_run_view(current, state)


@router.get("/schedules")
async def schedules(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    return {"schedules": [
        schedule_view(value) for value in await WorkflowScheduleService(service(request)).list(user.internal_user_id)
    ]}


@router.post("/schedules", status_code=201)
async def create_schedule(
    request: Request, body: ScheduleRequest, user: AuthenticatedUser = Depends(get_current_user),
):
    with constrain_request(tools=body.allowTools, automatic_memory=body.allowAutomaticMemory):
        value = await WorkflowScheduleService(service(request)).save(
            user, body.selection, body.input, body.limits, body.rule, body.idempotencyKey,
        )
    return schedule_view(value)


@router.put("/schedules/{schedule_id}")
async def update_schedule(
    request: Request, schedule_id: str, body: ScheduleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    with constrain_request(tools=body.allowTools, automatic_memory=body.allowAutomaticMemory):
        value = await WorkflowScheduleService(service(request)).save(
            user, body.selection, body.input, body.limits, body.rule, body.idempotencyKey,
            schedule_id=schedule_id, expected_revision=body.expectedRevision,
        )
    return schedule_view(value)


@router.post("/schedules/{schedule_id}/disable")
async def disable_schedule(
    request: Request, schedule_id: str, body: RevisionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    value = await WorkflowScheduleService(service(request)).disable(
        user.internal_user_id, schedule_id, body.expectedRevision,
    )
    return schedule_view(value)


@router.post("/schedules/{schedule_id}/recover-start")
async def recover_schedule_start(
    request: Request, schedule_id: str, user: AuthenticatedUser = Depends(get_current_user),
):
    current = service(request)
    current.require_enabled(scheduling=True)
    existing = await current.store.read_schedule(user.internal_user_id, schedule_id)
    if existing is None:
        raise AutomationError("schedule_missing", "The schedule is unavailable.", status=404)
    return schedule_view(await WorkflowScheduleService(current).ensure_started(existing.value))
