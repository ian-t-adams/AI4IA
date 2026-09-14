"""Explicit owner-only automation APIs; no standing workflow approval endpoint."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..sessions.repository import SessionNotFoundError
from ..workflows.automation_access import WorkflowSelection
from ..workflows.automation_common import AutomationError, AutomationModel, ExecutionLimits, MAX_SCHEDULES
from ..workflows.automation_models import WorkflowCheckpoint, WorkflowSchedule
from ..workflows.automation_service import WorkflowAutomationService
from ..workflows.schedule_service import WorkflowScheduleService
from ..workflows.scheduling import ScheduleRule

router = APIRouter(prefix="/api/workflows/automation", tags=["workflow automation"])


class StartRequest(AutomationModel):
    selection: WorkflowSelection
    input: str = Field(min_length=1, max_length=8000)
    limits: ExecutionLimits
    idempotencyKey: str = Field(min_length=32, max_length=128)
    sessionId: str | None = Field(default=None, max_length=128)


class ScheduleRequest(AutomationModel):
    selection: WorkflowSelection
    input: str = Field(min_length=1, max_length=8000)
    limits: ExecutionLimits
    rule: ScheduleRule
    idempotencyKey: str = Field(min_length=32, max_length=128)
    expectedRevision: int | None = Field(default=None, ge=0, strict=True)


class RevisionRequest(AutomationModel):
    expectedRevision: int = Field(ge=0, strict=True)


class DecisionRequest(AutomationModel):
    decision: Literal["approve", "deny"]
    requestId: str | None = Field(default=None, max_length=128)
    grant: str | None = Field(default=None, max_length=256)


def service(request: Request) -> WorkflowAutomationService:
    return request.app.state.workflow_automation


def run_view(state: WorkflowCheckpoint) -> dict[str, Any]:
    draft = state.draft
    return {
        "runId": state.runId, "sessionId": state.sessionId, "status": state.status,
        "reason": state.reason, "revision": state.revision,
        "workflow": state.bundle.workflow.displayName if state.bundle else "Workflow",
        "deadline": state.deadline.isoformat(), "step": state.step,
        "approval": {
            "id": draft.id, "tool": draft.canonicalTool, "label": draft.label,
            "risk": draft.risk, "purpose": draft.purpose, "destination": draft.destination,
            "expiresAt": draft.expiresAt.isoformat(), "state": draft.state,
            "argumentsDigest": draft.argumentsDigest,
        } if draft else None,
    }


def schedule_view(value: WorkflowSchedule) -> dict[str, Any]:
    return {
        "id": value.scheduleId, "revision": value.revision, "generation": value.generation,
        "enabled": value.enabled, "status": value.status, "reason": value.reason,
        "workflow": value.bundle.workflow.displayName, "workflowName": value.bundle.workflow.name,
        "source": value.bundle.source, "model": value.bundle.modelId,
        "input": value.input, "limits": value.limits.model_dump(mode="json"),
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
        result.append(run_view(state) if state is not None else {
            "runId": handle.runId, "sessionId": handle.sessionId,
            "workflow": "Workflow preparation", "status": "preparation_unavailable",
            "reason": "The conversation or checkpoint is unavailable. Stop this preparation before starting again.",
            "revision": 0, "deadline": None, "step": 0, "approval": None,
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
    }


@router.post("/runs", status_code=202)
async def start(
    request: Request, body: StartRequest, user: AuthenticatedUser = Depends(get_current_user),
):
    current = service(request)
    current.require_enabled()
    bundle = await current.access.freeze(
        user, body.selection, body.limits,
        session_id=body.sessionId or "automation-admission", safe_only=False,
    )
    result = await current.start(
        bundle, body.input, body.limits, body.idempotencyKey,
        user=user, session_id=body.sessionId,
    )
    return run_view(result)


@router.get("/runs/{run_id}")
async def read_run(request: Request, run_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    state, message = await service(request).load(user.internal_user_id, run_id)
    return {**run_view(state), "message": message.model_dump(mode="json")}


@router.get("/runs")
async def active_runs(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    return {"runs": await active_run_views(service(request), user.internal_user_id)}


@router.post("/runs/{run_id}/recover-start", status_code=202)
async def recover_run_start(request: Request, run_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    return run_view(await service(request).recover_start(user.internal_user_id, run_id))


@router.post("/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    state = await service(request).cancel(user.internal_user_id, run_id)
    return run_view(state) if state else {"runId": run_id, "status": "cancelled", "preparationOnly": True}


@router.get("/approvals")
async def approvals(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    return {"runs": await active_run_views(service(request), user.internal_user_id)}


@router.post("/runs/{run_id}/approvals/{draft_id}/review")
async def review(
    request: Request, run_id: str, draft_id: str, user: AuthenticatedUser = Depends(get_current_user),
):
    state, grant = await service(request).review(user.internal_user_id, run_id, draft_id, user)
    assert state.draft is not None and state.draft.challenge is not None
    return {
        **run_view(state), "argumentsJson": state.draft.argumentsJson,
        "requestId": state.draft.challenge.id, "grant": grant,
        "approvedDigest": state.bundle.approvedBundleDigest if state.bundle else None,
        "effectiveDigest": state.bundle.bundleDigest if state.bundle else None,
        "spendImpact": "Additional tool or provider spend is unknown. No hard dollar cap is enforced.",
    }


@router.post("/runs/{run_id}/approvals/{draft_id}/decision", status_code=202)
async def decide(
    request: Request, run_id: str, draft_id: str, body: DecisionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    state = await service(request).decide(
        user.internal_user_id, run_id, draft_id, user=user, decision=body.decision,
        request_id=body.requestId, grant=body.grant,
    )
    return run_view(state)


@router.get("/schedules")
async def schedules(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    return {"schedules": [
        schedule_view(value) for value in await WorkflowScheduleService(service(request)).list(user.internal_user_id)
    ]}


@router.post("/schedules", status_code=201)
async def create_schedule(
    request: Request, body: ScheduleRequest, user: AuthenticatedUser = Depends(get_current_user),
):
    value = await WorkflowScheduleService(service(request)).save(
        user, body.selection, body.input, body.limits, body.rule, body.idempotencyKey,
    )
    return schedule_view(value)


@router.put("/schedules/{schedule_id}")
async def update_schedule(
    request: Request, schedule_id: str, body: ScheduleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
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
