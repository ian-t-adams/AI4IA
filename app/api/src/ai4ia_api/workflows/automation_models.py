"""Canonical state for new workflow runs; no publication or group authority."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, TypeVar

from pydantic import Field, model_validator

from ..agents.agent_catalog import AgentCatalog
from ..agents.approvals import PendingToolApproval
from ..agents.turn_checkpoint import TurnCheckpoint
from ..catalog import DeploymentOption
from ..memory.context_refs import MemoryContextBinding
from ..publishing.models import EffectiveSubset
from ..usage.models import TokenUsage, UsageRecord
from .automation_common import (
    MAX_ACTIVE_RUNS, MAX_ARGUMENT_BYTES, MAX_SCHEDULES, MAX_SCHEDULE_HISTORY,
    MAX_STATE_BYTES, SHA256_PATTERN, TRANSITION_RESERVE_BYTES,
    AutomationError, AutomationModel, ExecutionLimits, digest, json_bytes, utc,
)
from .models import MAX_RUN_INPUT_LEN, MAX_STEPS, Workflow
from .runner import WorkflowStepResult
from .scheduling import ScheduleOccurrence, ScheduleRule

CHECKPOINT_KIND = "workflow_checkpoint_v3"
TERMINAL_STATES = frozenset({
    "completed", "failed", "cancelled", "denied", "expired", "timed_out",
    "context_revoked", "policy_revoked", "outcome_unknown",
})
RunStatus = Literal[
    "pending", "acceptance_unknown", "running", "awaiting_approval",
    "reauthentication_required", "accounting_pending", "completed", "failed",
    "cancelled", "denied", "expired", "timed_out", "context_revoked",
    "policy_revoked", "outcome_unknown",
]


class FrozenWorkflow(AutomationModel):
    executionOwnerId: str
    workflow: Workflow
    agents: AgentCatalog
    # A publishing-service reference or owned-draft revision/digest snapshot.
    # Only the access adapter creates this; this is never parsed as permission.
    source: dict[str, Any]
    modelId: str
    deployment: DeploymentOption
    api: str
    bundleDigest: str = Field(pattern=SHA256_PATTERN)
    approvedBundleDigest: str = Field(pattern=SHA256_PATTERN)
    effectiveSubsets: list[EffectiveSubset] = Field(default_factory=list, max_length=MAX_STEPS)
    environmentDigest: str = Field(pattern=SHA256_PATTERN)
    toolContracts: dict[str, str]
    stepContracts: list[dict[str, str]] = Field(max_length=MAX_STEPS)
    toolDestinations: dict[str, str | None]
    selectedDocuments: list[str] = Field(max_length=20)
    memoryStamp: str | None
    resourceStamps: dict[str, str]
    safeOnly: bool
    nonce: str


class InvocationDraft(AutomationModel):
    id: str
    operationId: str
    runId: str
    step: int = Field(ge=0, lt=MAX_STEPS)
    iteration: int = Field(ge=1, le=3)
    ordinal: int = Field(ge=0, le=7)
    tool: str
    canonicalTool: str
    label: str
    purpose: str
    risk: str
    contractDigest: str = Field(pattern=SHA256_PATTERN)
    bundleDigest: str = Field(pattern=SHA256_PATTERN)
    argumentsDigest: str = Field(pattern=SHA256_PATTERN)
    argumentsJson: str = Field(max_length=MAX_ARGUMENT_BYTES)
    destination: str | None
    createdAt: datetime
    expiresAt: datetime
    state: Literal["pending", "approved", "denied", "expired", "dispatched"]
    challenge: PendingToolApproval | None
    challengeGeneration: int = Field(ge=0, strict=True)
    decidedAt: datetime | None

    @model_validator(mode="after")
    def valid_lifetime(self) -> InvocationDraft:
        if utc(self.expiresAt) <= utc(self.createdAt):
            raise ValueError("An approval expiry must follow its creation.")
        if self.challenge is not None and utc(self.challenge.expiresAt) > utc(self.expiresAt):
            raise ValueError("A review challenge cannot extend its draft.")
        return self


class CompletedWorkflowStep(AutomationModel):
    result: WorkflowStepResult
    usage: TokenUsage


class WorkflowCheckpoint(AutomationModel):
    id: str
    kind: Literal["workflow_checkpoint_v3"] = CHECKPOINT_KIND
    userId: str
    sessionId: str
    deletionEpoch: str
    ownerEpoch: str
    runId: str
    fingerprint: str = Field(pattern=SHA256_PATTERN)
    revision: int = Field(ge=0, strict=True)
    status: RunStatus
    reason: str | None
    createdAt: datetime
    deadline: datetime
    leaseId: str | None
    leaseExpiresAt: datetime | None
    bundle: FrozenWorkflow | None
    input: str = Field(max_length=MAX_RUN_INPUT_LEN)
    limits: ExecutionLimits
    allowTools: bool = Field(strict=True)
    allowAutomaticMemory: bool = Field(strict=True)
    step: int = Field(ge=0, le=MAX_STEPS)
    previous: str
    completedSteps: list[CompletedWorkflowStep] = Field(max_length=MAX_STEPS)
    currentResult: WorkflowStepResult | None
    currentUsage: TokenUsage | None
    memoryContext: MemoryContextBinding | None
    turn: TurnCheckpoint | None
    draft: InvocationDraft | None
    approvalHistory: list[InvocationDraft] = Field(max_length=MAX_STEPS * 4)
    operationId: str | None
    operationState: Literal["idle", "reserved", "dispatched", "complete", "unknown"]
    scheduleId: str | None
    scheduleGeneration: int | None
    wakeRevision: int = Field(ge=0, strict=True)
    ttl: Literal[-1] = -1

    @model_validator(mode="after")
    def valid_state(self) -> WorkflowCheckpoint:
        utc(self.createdAt)
        utc(self.deadline)
        if self.status not in TERMINAL_STATES and self.bundle is None:
            raise ValueError("An active run has no frozen execution bundle.")
        if self.bundle is not None and self.bundle.executionOwnerId != self.userId:
            raise ValueError("The frozen execution owner does not match.")
        if self.draft is not None and self.draft.runId != self.runId:
            raise ValueError("The approval belongs to a different run.")
        return self


class RunHandle(AutomationModel):
    runId: str
    sessionId: str
    checkpointId: str
    fingerprint: str = Field(pattern=SHA256_PATTERN)
    workflowKey: str
    idempotencyKey: str
    createdAt: datetime
    active: bool
    terminal: bool
    modelCalls: int = Field(ge=0, strict=True)
    toolCalls: int = Field(ge=0, strict=True)
    dispatches: int = Field(ge=0, strict=True)
    operationFloor: int = Field(ge=-1, strict=True)
    scheduleId: str | None
    scheduleGeneration: int | None


class EffectIntent(AutomationModel):
    id: str
    runId: str
    sessionId: str
    operationId: str
    category: Literal["model", "tool", "dispatch"]
    payloadDigest: str = Field(pattern=SHA256_PATTERN)
    state: Literal["reserved", "dispatched", "complete", "unknown"]
    startedAt: datetime
    resultDigest: str | None
    usage: UsageRecord | None
    delivered: bool


class AutomationOwner(AutomationModel):
    id: str
    userId: str
    recordKind: str
    epoch: str
    revision: int = Field(ge=0, strict=True)
    requestFloor: datetime
    runs: dict[str, RunHandle] = Field(max_length=512)
    schedules: list[str] = Field(max_length=MAX_SCHEDULES)
    effects: dict[str, EffectIntent] = Field(max_length=MAX_ACTIVE_RUNS * 128)
    ttl: Literal[-1] = -1

    @model_validator(mode="after")
    def owned_effects(self) -> AutomationOwner:
        if sum(item.active for item in self.runs.values()) > MAX_ACTIVE_RUNS:
            raise ValueError("The active-run limit was exceeded.")
        for key, handle in self.runs.items():
            if key != handle.runId:
                raise ValueError("A run handle identity does not match.")
        for key, effect in self.effects.items():
            if key != effect.id or effect.runId not in self.runs:
                raise ValueError("An effect has no matching run handle.")
            if effect.usage is not None and effect.usage.userId != self.userId:
                raise ValueError("Usage belongs to a different owner.")
        return self


class ScheduleHistory(AutomationModel):
    slot: str
    dueAt: datetime
    outcome: Literal["launched", "missed", "overlap", "blocked"]
    runId: str | None


class WorkflowSchedule(AutomationModel):
    id: str
    userId: str
    recordKind: str
    scheduleId: str
    generation: int = Field(ge=1, strict=True)
    revision: int = Field(ge=0, strict=True)
    enabled: bool
    status: Literal["pending", "active", "acceptance_unknown", "paused", "completed", "disabled"]
    reason: str | None
    bundle: FrozenWorkflow
    input: str = Field(min_length=1, max_length=MAX_RUN_INPUT_LEN)
    limits: ExecutionLimits
    allowTools: bool = Field(strict=True)
    allowAutomaticMemory: bool = Field(strict=True)
    rule: ScheduleRule
    next: ScheduleOccurrence | None
    pendingSlot: ScheduleOccurrence | None
    pendingKey: str | None
    consumed: int = Field(ge=0, le=366, strict=True)
    lastSlot: str | None
    controllerId: str
    history: list[ScheduleHistory] = Field(max_length=MAX_SCHEDULE_HISTORY)
    createdAt: datetime
    updatedAt: datetime
    ttl: Literal[-1] = -1


StoredModel = TypeVar("StoredModel", bound=AutomationModel)


def persisted_model(model: type[StoredModel], raw: Any) -> StoredModel:
    if not isinstance(raw, Mapping) or not set(model.model_fields).issubset(raw):
        raise AutomationError("state_corrupt", "Required workflow coordination state is missing.")
    clean = {key: value for key, value in raw.items() if not key.startswith("_")}
    result = model.model_validate(clean)
    _require_shape(result.model_dump(mode="json"), clean)
    if isinstance(result, WorkflowCheckpoint) and raw.get("turn") is not None:
        TurnCheckpoint.from_persisted(raw["turn"])
    json_bytes(result.model_dump(mode="json"))
    return result


def _require_shape(template: Any, raw: Any) -> None:
    if isinstance(template, dict):
        if not isinstance(raw, dict) or not set(template).issubset(raw):
            raise AutomationError("state_corrupt", "Required nested workflow state is missing.")
        for key, child in template.items():
            _require_shape(child, raw[key])
    elif isinstance(template, list):
        if not isinstance(raw, list) or len(raw) != len(template):
            raise AutomationError("state_corrupt", "Workflow state has an incomplete collection.")
        for child, source in zip(template, raw, strict=True):
            _require_shape(child, source)
    elif isinstance(template, bool) and not isinstance(raw, bool):
        raise AutomationError("state_corrupt", "Workflow state has an invalid Boolean.")
    elif type(template) is int and (not isinstance(raw, int) or isinstance(raw, bool)):
        raise AutomationError("state_corrupt", "Workflow state has an invalid integer.")


def writable_body(model: AutomationModel, *, reserve: bool = True) -> dict[str, Any]:
    body = type(model).model_validate(model.model_dump(mode="json")).model_dump(mode="json")
    reserved = TRANSITION_RESERVE_BYTES if reserve else 0
    if isinstance(model, AutomationOwner):
        for effect in model.effects.values():
            if effect.usage is not None:
                json_bytes(effect.usage.model_dump(mode="json"), limit=8192)
            if effect.category == "dispatch" and effect.state in {"reserved", "dispatched", "unknown"}:
                reserved += 8192
    json_bytes(body, limit=MAX_STATE_BYTES - reserved)
    return body


def checkpoint_id(run_id: str) -> str:
    return "wf-state-" + digest(run_id)
