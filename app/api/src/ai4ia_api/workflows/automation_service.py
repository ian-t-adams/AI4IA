"""Owner-scoped automation lifecycle, backed by existing Cosmos partitions."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

from ..agents.approvals import consume_grant, draft_for_call, mint_pending_approval
from ..auth.base import AuthenticatedUser
from ..policy.context import unattended_policy_scope
from ..policy.models import PolicyError
from ..request_constraints import automatic_memory_allowed, constrain_request, fresh_session_required, tools_allowed
from ..sessions.models import Message, MessageRole, Session
from ..sessions.repository import SessionNotFoundError
from ..sessions.deletion_models import DeletionMigrationRequiredError
from .automation_access import WorkflowAccess
from .automation_common import (
    MAX_ACTIVE_RUNS, MISSED_GRACE_SECONDS, AutomationError, ExecutionLimits,
    digest, exact_arguments, request_time, stable_id, utc,
)
from .automation_models import (
    TERMINAL_STATES, AutomationOwner, CompletedWorkflowStep, FrozenWorkflow,
    RunHandle, RunStatus, WorkflowCheckpoint, checkpoint_id,
)
from .automation_receipts import project_message
from .automation_store import AutomationStore, Stored
from .dispatch_scope import workflow_execution_scope
from .durable import (
    DurableScheduleAcceptanceUnknownError, DurableScheduleRejectedError,
    _truncate_for_payload, durable_message_ids, durable_run_id,
)
from .runner import MAX_CARRY_LEN, run_workflow_step

logger = logging.getLogger(__name__)

class AutomationHost(Protocol):
    async def start_run(self, state: WorkflowCheckpoint) -> None: ...
    async def wake(self, owner: str, run_id: str, revision: int) -> None: ...
    async def start_schedule(self, owner: str, schedule_id: str, generation: int, controller_id: str) -> None: ...


class WorkflowAutomationService:
    def __init__(self, state: Any, store: AutomationStore, access: WorkflowAccess) -> None:
        self.state = state
        self.store = store
        self.access = access
        self.host: AutomationHost | None = None

    def require_enabled(self, *, scheduling: bool = False) -> None:
        settings = self.state.settings
        if not getattr(settings, "workflow_approvals_enabled", False) or (
            scheduling and not getattr(settings, "workflow_scheduling_enabled", False)
        ):
            raise AutomationError("feature_disabled", "Workflow automation is disabled.", status=404)
        if settings.hard_quota_enabled:
            raise AutomationError(
                "hard_quota_durable_unsupported", "Hard-quota durable execution remains unsupported.",
            )
        if self.host is None or not self.state.usage.enabled:
            raise AutomationError("automation_unavailable", "Durable execution or usage is unavailable.", status=503)

    async def owner(self, owner_id: str, *, create: bool = False) -> Stored[AutomationOwner]:
        current = await self.store.read_owner(owner_id)
        if current is None and create:
            await self.store.create_owner(AutomationOwner(
                id=self.store.owner_id, userId=owner_id, recordKind=self.store.owner_kind,
                epoch=uuid4().hex, revision=0,
                requestFloor=datetime(1970, 1, 1, tzinfo=timezone.utc),
                runs={}, schedules=[], effects={},
            ))
            current = await self.store.read_owner(owner_id)
        if current is None:
            raise AutomationError("coordination_missing", "Workflow coordination is unavailable.", status=503)
        return current

    async def mutate_owner(
        self, owner: str, change: Callable[[AutomationOwner, datetime], None],
    ) -> Stored[AutomationOwner]:
        for _ in range(3):
            prior = await self.owner(owner)
            updated = prior.value.model_copy(deep=True)
            change(updated, prior.now)
            updated.revision = prior.value.revision + 1
            if await self.store.write_owner(prior, updated):
                return Stored(updated, "", prior.now)
        raise AutomationError("coordination_contended", "Workflow state changed concurrently.", status=503)

    @staticmethod
    def compact(owner: AutomationOwner, now: datetime) -> None:
        floor = now - timedelta(days=30)
        removable = {
            key for key, run in owner.runs.items()
            if run.terminal and not run.active and request_time(run.idempotencyKey) < floor
            and all(
                effect.state == "complete" and (effect.usage is None or effect.delivered)
                for effect in owner.effects.values() if effect.runId == key
            )
        }
        for key in removable:
            del owner.runs[key]
        owner.effects = {key: value for key, value in owner.effects.items() if value.runId not in removable}
        owner.requestFloor = max(owner.requestFloor, floor)

    async def start(
        self, bundle: FrozenWorkflow, text: str, limits: ExecutionLimits, key: str, *,
        user: AuthenticatedUser | None, session_id: str | None = None,
        schedule_id: str | None = None, schedule_generation: int | None = None,
    ) -> WorkflowCheckpoint:
        self.require_enabled(scheduling=schedule_id is not None)
        if fresh_session_required():
            raise AutomationError("request_restricted", "A fresh canary request cannot create durable workflow authority.")
        await self.access.recheck(bundle, user=user)
        # An interactive start is not a durable delegation of that JWT.
        await self.access.recheck(bundle, user=None)
        unattended = await self.access.actor(bundle.executionOwnerId, None)
        for index in range(len(bundle.workflow.steps)):
            await self.access.surface(
                unattended, bundle.workflow, bundle.agents, index,
                session_id=session_id or "automation-admission",
                documents=bundle.selectedDocuments, nonce=bundle.nonce, safe_only=bundle.safeOnly,
            )
        text = text.strip()
        if not text or len(text) > 8000:
            raise AutomationError("invalid_input", "Workflow input must contain 1-8000 characters.", status=422)
        if session_id is not None:
            requested_session = await self.state.session_repo.get_session(bundle.executionOwnerId, session_id)
            if requested_session.deletionProtocol != 1 or requested_session.deletionEpoch is None:
                raise AutomationError("session_protocol_required", "Use a new protocol-v1 workflow conversation.")
        issued = request_time(key)
        owner = await self.owner(bundle.executionOwnerId, create=True)
        if issued > owner.now + timedelta(minutes=5) or issued < owner.now - timedelta(days=30):
            raise AutomationError("stale_invocation", "This invocation key is outside its recovery horizon.")
        run_id = durable_run_id(bundle.executionOwnerId, key, scope="automation-v3")
        session_id = session_id or "wfs-" + stable_id(bundle.executionOwnerId, run_id)[:40]
        fingerprint = digest({
            "bundle": bundle.model_dump(mode="json", exclude={"nonce"}),
            "input": text, "limits": limits.model_dump(mode="json"), "session": session_id,
            "schedule": schedule_id, "generation": schedule_generation,
            "tools": tools_allowed(), "automaticMemory": automatic_memory_allowed(),
        })
        workflow_key = digest([bundle.workflow.userId, bundle.workflow.name])

        def claim(value: AutomationOwner, now: datetime) -> None:
            self.compact(value, now)
            existing = value.runs.get(run_id)
            if existing is not None:
                if existing.fingerprint != fingerprint or existing.sessionId != session_id:
                    raise AutomationError("invocation_conflict", "The invocation key is bound to different work.")
                return
            if issued < value.requestFloor:
                raise AutomationError("stale_invocation", "The invocation key has already aged out.")
            if len(value.runs) >= 512 or sum(item.active for item in value.runs.values()) >= MAX_ACTIVE_RUNS:
                raise AutomationError("run_limit", "The durable run/history capacity is exhausted.")
            if any(item.active and item.workflowKey == workflow_key for item in value.runs.values()):
                raise AutomationError("overlap_denied", "This workflow already has an active or unresolved run.")
            value.runs[run_id] = RunHandle(
                runId=run_id, sessionId=session_id, checkpointId=checkpoint_id(run_id),
                fingerprint=fingerprint, workflowKey=workflow_key, idempotencyKey=key,
                createdAt=now, active=True, terminal=False, modelCalls=0, toolCalls=0, dispatches=0,
                operationFloor=-1, scheduleId=schedule_id, scheduleGeneration=schedule_generation,
            )

        if schedule_id is None:
            owner = await self.mutate_owner(bundle.executionOwnerId, claim)
        else:
            for _ in range(3):
                prior_owner = await self.owner(bundle.executionOwnerId)
                if run_id in prior_owner.value.runs:
                    owner = await self.mutate_owner(bundle.executionOwnerId, claim)
                    break
                scheduled = await self.store.read_schedule(bundle.executionOwnerId, schedule_id)
                if (
                    scheduled is None or not scheduled.value.enabled
                    or scheduled.value.generation != schedule_generation
                    or scheduled.value.pendingKey != key
                    or scheduled.value.bundle.bundleDigest != bundle.bundleDigest
                ):
                    raise AutomationError("schedule_revoked", "This occurrence is no longer authorized.")
                if scheduled.value.pendingSlot is None or prior_owner.now > (
                    scheduled.value.pendingSlot.dueAt + timedelta(seconds=MISSED_GRACE_SECONDS)
                ):
                    raise AutomationError("occurrence_missed", "The unadmitted occurrence is outside its grace interval.")
                updated_owner = prior_owner.value.model_copy(deep=True)
                claim(updated_owner, prior_owner.now)
                updated_owner.revision += 1
                same_schedule = scheduled.value.model_copy(update={"revision": scheduled.value.revision + 1}, deep=True)
                if await self.store.write_schedule(prior_owner, updated_owner, same_schedule, scheduled):
                    owner = Stored(updated_owner, "", prior_owner.now)
                    break
            else:
                raise AutomationError("schedule_changed", "The occurrence admission is contended.", status=503)
        handle = owner.value.runs[run_id]
        try:
            session = await self.state.session_repo.get_session(bundle.executionOwnerId, session_id)
        except SessionNotFoundError:
            session = await self.state.session_repo.create_session(Session(
                id=session_id, userId=bundle.executionOwnerId, model=bundle.modelId,
                title=f"Workflow: {bundle.workflow.displayName}",
                libraryDocumentIds=list(bundle.selectedDocuments),
            ))
        if session.deletionProtocol != 1 or session.deletionEpoch is None:
            raise AutomationError("session_protocol_required", "Use a new protocol-v1 workflow conversation.")
        existing = await self.state.session_repo.read_workflow_checkpoint(
            bundle.executionOwnerId, session_id, handle.checkpointId,
        )
        if existing is not None:
            if existing.fingerprint != fingerprint or existing.ownerEpoch != owner.value.epoch:
                raise AutomationError("invocation_conflict", "The existing run has a different source binding.")
            if existing.status not in {"pending", "acceptance_unknown"}:
                return existing
            state = existing
        else:
            state = WorkflowCheckpoint(
                id=handle.checkpointId, userId=bundle.executionOwnerId, sessionId=session_id,
                deletionEpoch=session.deletionEpoch, ownerEpoch=owner.value.epoch,
                runId=run_id, fingerprint=fingerprint, revision=0, status="pending", reason=None,
                createdAt=handle.createdAt, deadline=handle.createdAt + timedelta(seconds=limits.maxRuntimeSeconds),
                leaseId=None, leaseExpiresAt=None, bundle=bundle, input=text, limits=limits,
                allowTools=tools_allowed(), allowAutomaticMemory=automatic_memory_allowed(),
                step=0, previous="", completedSteps=[], currentResult=None, currentUsage=None,
                memoryContext=None, turn=None, draft=None, approvalHistory=[],
                operationId=None, operationState="idle", scheduleId=schedule_id,
                scheduleGeneration=schedule_generation, wakeRevision=0,
            )
            user_id, _ = durable_message_ids(run_id)
            user_message = Message(
                id=user_id, userId=state.userId, sessionId=session_id, role=MessageRole.user,
                content=text, agent=f"workflow:{bundle.workflow.name}",
                workflowRunId=run_id, workflowRunFingerprint=fingerprint,
                createdAt=state.createdAt - timedelta(microseconds=1),
            )
            if not await self.state.session_repo.claim_workflow_checkpoint(
                state.userId, user_message, project_message(state), state,
            ):
                state, _ = await self.load(state.userId, run_id)
        if self.host is None:
            raise AutomationError("automation_unavailable", "The durable host is unavailable.", status=503)
        try:
            await self.host.start_run(state)
        except DurableScheduleAcceptanceUnknownError:
            return await self.set_status(
                state.userId, run_id, "acceptance_unknown", "scheduler_ack_unknown",
                only_from=frozenset({"pending", "acceptance_unknown"}),
            )
        except DurableScheduleRejectedError:
            failed = await self.set_status(
                state.userId, run_id, "failed", "schedule_rejected",
                only_from=frozenset({"pending", "acceptance_unknown"}),
            )
            if failed.status == "failed" and failed.reason == "schedule_rejected":
                await self.finish_handle(state.userId, run_id)
            return failed
        return state

    async def load(self, owner: str, run_id: str) -> tuple[WorkflowCheckpoint, Message]:
        if run_id.partition(":")[0] != owner:
            raise AutomationError("run_not_found", "The run is unavailable.", status=404)
        current = await self.owner(owner)
        handle = current.value.runs.get(run_id)
        if handle is None or run_id.partition(":")[0] != owner:
            raise AutomationError("run_not_found", "The run is unavailable.", status=404)
        state = await self.state.session_repo.read_workflow_checkpoint(owner, handle.sessionId, handle.checkpointId)
        if state is None or state.fingerprint != handle.fingerprint or state.ownerEpoch != current.value.epoch:
            raise AutomationError("coordination_missing", "The run's canonical checkpoint is unavailable.", status=503)
        _, assistant_id = durable_message_ids(run_id)
        messages = await self.state.session_repo.list_messages(owner, handle.sessionId)
        message = next((item for item in messages if item.id == assistant_id), None)
        if message is None:
            raise AutomationError("context_revoked", "The run conversation was cleared.", status=404)
        return state, message.model_copy(deep=True)

    async def commit(
        self, expected: WorkflowCheckpoint, previous: Message, updated: WorkflowCheckpoint,
    ) -> tuple[WorkflowCheckpoint, Message]:
        updated.revision = expected.revision + 1
        message = project_message(updated, previous)
        if not await self.state.session_repo.replace_workflow_checkpoint(
            expected.userId, updated, message, expected=expected, expected_assistant=previous,
        ):
            raise AutomationError("checkpoint_changed", "The run changed before this transition.")
        return updated, message

    async def set_status(
        self, owner: str, run_id: str, status: RunStatus, reason: str | None,
        *, only_from: frozenset[str] | None = None,
    ) -> WorkflowCheckpoint:
        for _ in range(3):
            state, message = await self.load(owner, run_id)
            if state.status in TERMINAL_STATES:
                return state
            if only_from is not None and state.status not in only_from:
                return state
            updated = state.model_copy(update={
                "status": status, "reason": reason, "leaseId": None, "leaseExpiresAt": None,
            }, deep=True)
            try:
                updated, _ = await self.commit(state, message, updated)
                return updated
            except AutomationError as exc:
                if exc.code != "checkpoint_changed":
                    raise
        raise AutomationError("checkpoint_changed", "The run changed concurrently.")

    async def flush_usage(self, owner: str, run_id: str) -> None:
        current = await self.owner(owner)
        for effect in current.value.effects.values():
            if effect.runId != run_id or effect.usage is None or effect.delivered:
                continue
            if effect.state not in {"complete", "unknown"}:
                continue
            await self.state.usage.record_frozen(effect.usage)

            def mark(value: AutomationOwner, now: datetime, identifier: str = effect.id) -> None:
                latest = value.effects.get(identifier)
                if latest is None or latest.usage != effect.usage:
                    raise AutomationError("accounting_changed", "The usage outbox changed.")
                latest.delivered = True

            await self.mutate_owner(owner, mark)

    async def finish_handle(self, owner: str, run_id: str) -> None:
        await self.flush_usage(owner, run_id)

        def finish(value: AutomationOwner, now: datetime) -> None:
            handle = value.runs[run_id]
            handle.terminal = True
            effects = [item for item in value.effects.values() if item.runId == run_id]
            handle.active = any(
                item.state in {"dispatched", "unknown"} or (item.usage is not None and not item.delivered)
                for item in effects
            )

        await self.mutate_owner(owner, finish)

    async def stop(self, owner: str, run_id: str, status: RunStatus, reason: str) -> WorkflowCheckpoint:
        def fence(value: AutomationOwner, now: datetime) -> None:
            handle = value.runs.get(run_id)
            if handle is None:
                raise AutomationError("run_not_found", "The run is unavailable.", status=404)
            handle.terminal = True

        await self.mutate_owner(owner, fence)
        state = await self.set_status(owner, run_id, status, reason)
        await self.finish_handle(owner, run_id)
        if self.host is not None:
            try:
                await self.host.wake(owner, run_id, state.revision)
            except (DurableScheduleAcceptanceUnknownError, OSError):
                # Canonical stop is already durable; its reconciliation timer
                # handles an undelivered hint without undoing the stop.
                logger.warning("workflow stop wake hint pending; durable reconciliation remains authoritative")
        return state

    async def pending(self, owner: str) -> list[WorkflowCheckpoint]:
        current = await self.store.read_owner(owner)
        if current is None:
            return []
        result = []
        for handle in current.value.runs.values():
            if not handle.active:
                continue
            try:
                state, _ = await self.load(owner, handle.runId)
            except SessionNotFoundError:
                continue
            if state.status in {"awaiting_approval", "reauthentication_required", "outcome_unknown", "accounting_pending"}:
                result.append(state)
        return result[:20]

    async def cancel(
        self, owner: str, run_id: str, *, status: RunStatus = "cancelled", reason: str = "owner_cancelled",
    ) -> WorkflowCheckpoint | None:
        try:
            return await self.stop(owner, run_id, status, reason)
        except (SessionNotFoundError, DeletionMigrationRequiredError):
            await self.finish_handle(owner, run_id)
            return None
        except AutomationError as exc:
            if exc.code not in {"coordination_missing", "context_revoked"}:
                raise
            # stop() has already fenced the owner handle. A preparation without
            # a child checkpoint can be cancelled without inventing that child.
            await self.finish_handle(owner, run_id)
            return None

    async def recover_start(self, owner: str, run_id: str) -> WorkflowCheckpoint:
        self.require_enabled()
        state, _ = await self.load(owner, run_id)
        if state.status not in {"pending", "acceptance_unknown"}:
            return state
        current = await self.owner(owner)
        if current.value.runs[run_id].terminal or state.bundle is None:
            raise AutomationError("execution_revoked", "The run no longer permits scheduling.")
        await self.access.recheck(state.bundle, user=None)
        await self.access.check_context(state)
        if self.host is None:
            raise AutomationError("automation_unavailable", "The durable host is unavailable.", status=503)
        try:
            await self.host.start_run(state)
        except DurableScheduleAcceptanceUnknownError:
            return await self.set_status(
                owner, run_id, "acceptance_unknown", "scheduler_ack_unknown",
                only_from=frozenset({"pending", "acceptance_unknown"}),
            )
        return (await self.load(owner, run_id))[0]

    async def review(self, owner: str, run_id: str, draft_id: str, user: AuthenticatedUser) -> tuple[WorkflowCheckpoint, str]:
        self.require_enabled()
        state, message = await self.load(owner, run_id)
        draft = state.draft
        now = (await self.owner(owner)).now
        if (
            state.status != "awaiting_approval" or message.workflowConsentRevoked
            or draft is None or draft.id != draft_id or draft.state != "pending"
            or now >= utc(draft.expiresAt)
        ):
            raise AutomationError("approval_unavailable", "This exact approval is no longer pending.")
        if state.bundle is None:
            raise AutomationError("context_revoked", "The execution source was cleared.")
        actor = await self.access.recheck(state.bundle, user=user)
        await self.access.check_context(state)
        surface = await self.access.surface(
            actor, state.bundle.workflow, state.bundle.agents, state.step,
            session_id=state.sessionId, documents=state.bundle.selectedDocuments,
            nonce=state.bundle.nonce, safe_only=state.bundle.safeOnly,
        )
        if surface.contracts != state.bundle.stepContracts[state.step]:
            raise AutomationError("policy_revoked", "The effective tool subset changed.")
        from ..agents.consent_service import describe_contracts

        target = state.bundle.agents.get(state.bundle.workflow.steps[state.step].agent)
        if target is None:
            raise AutomationError("workflow_unavailable", "The step is unavailable.")
        names = list(dict.fromkeys([*target.tools, *state.bundle.workflow.steps[state.step].extraTools]))
        described = await describe_contracts(
            self.state, user_id=owner, tool_names=names, schemas=surface.schemas.tools,
            publication_metadata=True,
        )
        contract = described.get(draft.tool)
        if contract is None or contract.digest != draft.contractDigest:
            raise AutomationError("policy_revoked", "The exact tool contract changed.")
        arguments, identity, _ = exact_arguments(
            draft.argumentsJson, visible_resource_ids=frozenset(state.bundle.selectedDocuments),
        )
        if identity != draft.argumentsDigest:
            raise AutomationError("state_corrupt", "The pending arguments no longer match.")
        challenge, grant = mint_pending_approval(
            draft_for_call(contract.spec, tool=draft.tool, label=draft.label, arguments=arguments),
            now=now, ttl_seconds=max(1, int((draft.expiresAt - now).total_seconds())),
        )
        updated = state.model_copy(deep=True)
        if updated.draft is None:
            raise AutomationError("approval_unavailable", "The pending approval changed.")
        updated.draft.challenge = challenge
        updated.draft.challengeGeneration += 1
        updated, _ = await self.commit(state, message, updated)
        return updated, grant

    async def decide(
        self, owner: str, run_id: str, draft_id: str, *, user: AuthenticatedUser,
        decision: str, request_id: str | None = None, grant: str | None = None,
    ) -> WorkflowCheckpoint:
        state, message = await self.load(owner, run_id)
        draft = state.draft
        now = (await self.owner(owner)).now
        if (
            state.status != "awaiting_approval" or message.workflowConsentRevoked
            or draft is None or draft.id != draft_id or draft.state != "pending"
        ):
            raise AutomationError("approval_unavailable", "This exact approval is no longer pending.")
        if now >= utc(draft.expiresAt):
            return await self.stop(owner, run_id, "expired", "approval_expired")
        if decision == "deny":
            return await self.stop(owner, run_id, "denied", "owner_denied")
        self.require_enabled()
        if not tools_allowed() or (state.memoryContext is not None and not automatic_memory_allowed()):
            return await self.stop(owner, run_id, "policy_revoked", "request_restricted")
        if decision != "approve" or state.bundle is None:
            raise AutomationError("invalid_decision", "Approve or deny this exact call.", status=422)
        await self.access.recheck(state.bundle, user=user)
        await self.access.check_context(state)
        challenge = draft.challenge
        if challenge is None or challenge.id != request_id or not consume_grant(challenge, grant, now=now).granted:
            raise AutomationError("grant_rejected", "The one-time approval is invalid, expired or already used.")
        updated = state.model_copy(deep=True)
        if updated.draft is None or updated.draft.challenge is None:
            raise AutomationError("approval_unavailable", "The pending approval changed.")
        updated.draft.challenge.consumed = True
        updated.draft.state = "approved"
        updated.draft.decidedAt = now
        updated.wakeRevision += 1
        updated, _ = await self.commit(state, message, updated)
        if self.host is not None:
            try:
                await self.host.wake(owner, run_id, updated.wakeRevision)
            except (DurableScheduleAcceptanceUnknownError, OSError):
                logger.warning("workflow approval wake hint pending; durable reconciliation remains authoritative")
        return updated

    async def advance(self, owner: str, run_id: str, *, user: AuthenticatedUser | None = None) -> dict[str, Any]:
        try:
            state, _ = await self.load(owner, run_id)
        except SessionNotFoundError:
            await self.finish_handle(owner, run_id)
            return {"terminal": True, "status": "cancelled"}
        except AutomationError as exc:
            if exc.code != "context_revoked":
                raise
            await self.finish_handle(owner, run_id)
            return {"terminal": True, "status": "cancelled"}
        with constrain_request(tools=state.allowTools, automatic_memory=state.allowAutomaticMemory):
            return await self._advance(owner, run_id, user=user)

    async def _advance(self, owner: str, run_id: str, *, user: AuthenticatedUser | None = None) -> dict[str, Any]:
        from .run_controller import RunController

        state, message = await self.load(owner, run_id)
        now = (await self.owner(owner)).now
        if state.status in TERMINAL_STATES:
            await self.finish_handle(owner, run_id)
            return {"terminal": True, "status": state.status}
        if message.workflowConsentRevoked:
            state = await self.stop(owner, run_id, "cancelled", "owner_cancelled")
            return {"terminal": True, "status": state.status}
        if now >= utc(state.deadline):
            state = await self.stop(owner, run_id, "timed_out", "runtime_exceeded")
            return {"terminal": True, "status": state.status}
        try:
            self.require_enabled(scheduling=state.scheduleId is not None)
        except AutomationError:
            state = await self.stop(owner, run_id, "policy_revoked", "feature_disabled")
            return {"terminal": True, "status": state.status}
        if state.draft is not None and state.draft.state == "pending":
            if now >= utc(state.draft.expiresAt):
                state = await self.stop(owner, run_id, "expired", "approval_expired")
                return {"terminal": True, "status": state.status}
            return {"terminal": False, "status": state.status, "waitUntil": state.draft.expiresAt.isoformat()}
        if state.leaseId is not None:
            if state.operationState in {"dispatched", "unknown"}:
                return {"terminal": False, "status": "running", "waitUntil": state.deadline.isoformat()}
            if state.leaseExpiresAt is not None and now < utc(state.leaseExpiresAt):
                return {"terminal": False, "status": "running", "waitUntil": state.leaseExpiresAt.isoformat()}
        if state.bundle is None:
            raise AutomationError("context_revoked", "The frozen run context is unavailable.")
        try:
            actor = await self.access.recheck(state.bundle, user=user)
            await self.access.check_context(state)
            surface = await self.access.surface(
                actor, state.bundle.workflow, state.bundle.agents, state.step,
                session_id=state.sessionId, documents=state.bundle.selectedDocuments,
                nonce=state.bundle.nonce, safe_only=state.bundle.safeOnly,
            )
            if surface.contracts != state.bundle.stepContracts[state.step]:
                raise AutomationError("policy_revoked", "The effective tool subset no longer matches.")
        except PolicyError as exc:
            status: RunStatus = "reauthentication_required" if exc.decision.reason == "reauthentication_required" else "policy_revoked"
            state = await self.set_status(owner, run_id, status, exc.decision.reason)
            return {"terminal": status in TERMINAL_STATES, "status": status, "waitUntil": state.deadline.isoformat()}
        except AutomationError as exc:
            state = await self.stop(owner, run_id, "context_revoked", exc.code)
            return {"terminal": True, "status": state.status}
        acquired = state.model_copy(update={
            "leaseId": uuid4().hex, "leaseExpiresAt": now + timedelta(seconds=30),
            "status": "running", "reason": None,
        }, deep=True)
        if acquired.operationState == "reserved" and acquired.turn is not None and acquired.turn.phase == "model":
            acquired.turn = acquired.turn.model_copy(update={
                "phase": "ready", "iterations": acquired.turn.iterations - 1,
                "modelRequests": acquired.turn.modelRequests[:-1],
            }, deep=True)
        state, message = await self.commit(state, message, acquired)
        control = RunController(self, state, message, user=user)
        bundle = state.bundle
        if bundle is None:
            raise AutomationError("context_revoked", "The execution source was cleared.")
        remaining = max(0.001, (state.deadline - now).total_seconds())
        try:
            with unattended_policy_scope(self.state.policy, owner), workflow_execution_scope(control):
                async with asyncio.timeout(remaining):
                    step = bundle.workflow.steps[state.step]
                    result = await run_workflow_step(
                        step, index=state.step, workflow_name=bundle.workflow.name,
                        run_input=state.input, previous=state.previous, composed=bundle.agents,
                        deployment=bundle.deployment.deploymentName, gateway=self.state.gateway,
                        registry=self.state.tool_registry, executor=self.state.tool_executor,
                        capabilities=lambda names: control.capabilities(list(names)),
                        tool_builder=control.tools, checkpoint=control,
                        invocation_approvals=control.approvals(),
                        model_params={"max_tokens": state.limits.maxOutputTokens},
                        model_id=bundle.modelId, api=bundle.api, pricing=self.state.usage.pricing,
                    )
        except TimeoutError:
            state = await self.stop(owner, run_id, "timed_out", "runtime_exceeded")
            return {"terminal": True, "status": state.status}
        try:
            state, message = await self.load(owner, run_id)
        except SessionNotFoundError:
            await self.finish_handle(owner, run_id)
            return {"terminal": True, "status": "cancelled"}
        except AutomationError as exc:
            if exc.code != "context_revoked":
                raise
            await self.finish_handle(owner, run_id)
            return {"terminal": True, "status": "cancelled"}
        if state.status in TERMINAL_STATES:
            same_operation = (
                state.step == control.current.step
                and state.operationId == control.current.operationId
                and state.turn == control.current.turn
            )
            if (
                state.bundle is not None and result.result.receipt is not None
                and (
                    (state == control.current and message == control.message)
                    or (state.status in {"cancelled", "timed_out", "denied", "expired"} and same_operation)
                )
            ):
                late = state.model_copy(update={
                    "currentResult": result.result, "currentUsage": result.usage,
                }, deep=True)
                state, _ = await self.commit(state, message, late)
            await self.finish_handle(owner, run_id)
            return {"terminal": True, "status": state.status}
        if state != control.current or message != control.message:
            return {
                "terminal": False, "status": state.status,
                "waitUntil": state.draft.expiresAt.isoformat() if state.draft else state.deadline.isoformat(),
            }
        if state.status == "reauthentication_required":
            return {"terminal": False, "status": state.status, "waitUntil": state.deadline.isoformat()}
        updated = state.model_copy(deep=True)
        updated.leaseId = None
        updated.leaseExpiresAt = None
        if result.paused:
            updated.currentResult = result.result
            updated.currentUsage = result.usage
            updated, _ = await self.commit(state, message, updated)
            return {"terminal": False, "status": updated.status, "waitUntil": updated.draft.expiresAt.isoformat() if updated.draft else updated.deadline.isoformat()}
        if result.fatal:
            updated.currentResult = result.result
            updated.currentUsage = result.usage
            updated.status = "failed"
            updated.reason = result.failure_code or "step_failed"
        else:
            last_step = updated.step + 1 == len(bundle.workflow.steps)
            updated.previous = (
                _truncate_for_payload(result.result.text, 24 * 1024)
                if last_step else result.result.text[:MAX_CARRY_LEN]
            )
            result.result.text = _truncate_for_payload(result.result.text, 4096)
            updated.completedSteps.append(CompletedWorkflowStep(result=result.result, usage=result.usage))
            updated.step += 1
            updated.turn = None
            updated.currentResult = None
            updated.currentUsage = None
            updated.operationId = None
            updated.operationState = "idle"
            if updated.step == len(bundle.workflow.steps):
                updated.status = "completed"
        updated, _ = await self.commit(state, message, updated)
        await self.flush_usage(owner, run_id)
        if updated.status in TERMINAL_STATES:
            await self.finish_handle(owner, run_id)
        return {"terminal": updated.status in TERMINAL_STATES, "status": updated.status, "reason": updated.reason}
