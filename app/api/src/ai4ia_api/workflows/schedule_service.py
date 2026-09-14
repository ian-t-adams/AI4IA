"""Finite calendar controllers with atomic owner/slot claims."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..auth.base import AuthenticatedUser
from ..request_constraints import automatic_memory_allowed, constrain_request, tools_allowed
from .automation_access import WorkflowSelection
from .automation_common import (
    MAX_SCHEDULES, MAX_SCHEDULE_HISTORY, MISSED_GRACE_SECONDS,
    AutomationError, ExecutionLimits, digest, request_time, stable_id,
)
from .automation_models import ScheduleHistory, WorkflowSchedule
from .automation_service import WorkflowAutomationService
from .durable import DurableScheduleAcceptanceUnknownError, DurableScheduleRejectedError
from .scheduling import ScheduleRule, first_occurrence, next_occurrence, zone_identity


class WorkflowScheduleService:
    def __init__(self, automation: WorkflowAutomationService) -> None:
        self.automation = automation
        self.store = automation.store

    async def list(self, owner: str) -> list[WorkflowSchedule]:
        state = await self.store.read_owner(owner)
        if state is None:
            return []
        schedules = []
        for identifier in state.value.schedules:
            row = await self.store.read_schedule(owner, identifier)
            if row is None:
                raise AutomationError("schedule_missing", "A registered schedule is unavailable.", status=503)
            schedules.append(row.value)
        return schedules

    async def save(
        self, user: AuthenticatedUser, selection: WorkflowSelection, text: str,
        limits: ExecutionLimits, rule: ScheduleRule, key: str, *,
        schedule_id: str | None = None, expected_revision: int | None = None,
    ) -> WorkflowSchedule:
        automation = self.automation
        automation.require_enabled(scheduling=True)
        owner = user.internal_user_id
        issued = request_time(key)
        current = await automation.owner(owner, create=True)
        if issued > current.now + timedelta(minutes=5) or issued < current.now - timedelta(days=30):
            raise AutomationError("stale_invocation", "This schedule request is outside its recovery horizon.")
        identifier = schedule_id or stable_id(owner, "schedule", key)[:32]
        bundle = await automation.access.freeze(
            user, selection, limits, session_id="schedule-admission", safe_only=True,
        )
        if not text.strip() or len(text) > 8000:
            raise AutomationError("invalid_input", "Schedule input must contain 1-8000 characters.", status=422)
        write_digest = digest({
            "selection": selection.model_dump(mode="json"), "input": text.strip(),
            "bundle": bundle.bundleDigest, "limits": limits.model_dump(mode="json"),
            "rule": rule.model_dump(mode="json"), "expectedRevision": expected_revision,
            "tools": tools_allowed(), "automaticMemory": automatic_memory_allowed(),
        })
        for _ in range(3):
            before_owner = await automation.owner(owner)
            before = await self.store.read_schedule(owner, identifier)
            if before is not None and before.value.lastWriteKey == key:
                if before.value.lastWriteDigest != write_digest:
                    raise AutomationError("schedule_changed", "The request key is bound to a different schedule.")
                return await self.ensure_started(before.value)
            if schedule_id is not None and (before is None or before.value.revision != expected_revision):
                raise AutomationError("schedule_changed", "Reload the schedule before editing it.")
            if before is not None and schedule_id is None:
                saved = before.value
                if (
                    saved.rule != rule or saved.input != text.strip() or saved.limits != limits
                    or saved.bundle.bundleDigest != bundle.bundleDigest
                ):
                    raise AutomationError("schedule_changed", "The request key is bound to a different schedule.")
                return await self.ensure_started(saved)
            updated_owner = before_owner.value.model_copy(deep=True)
            if identifier not in updated_owner.schedules:
                if len(updated_owner.schedules) >= MAX_SCHEDULES:
                    raise AutomationError("schedule_limit", "The owner schedule capacity was reached.")
                updated_owner.schedules.append(identifier)
            updated_owner.revision += 1
            generation = before.value.generation + 1 if before else 1
            value = WorkflowSchedule(
                id=self.store.schedule_prefix + identifier, userId=owner,
                recordKind=self.store.schedule_kind, scheduleId=identifier,
                generation=generation, revision=before.value.revision + 1 if before else 0,
                lastWriteKey=key, lastWriteDigest=write_digest,
                enabled=True, status="pending", reason=None, bundle=bundle,
                input=text.strip(), limits=limits, rule=rule,
                allowTools=tools_allowed(), allowAutomaticMemory=automatic_memory_allowed(),
                next=first_occurrence(rule, before_owner.now), pendingSlot=None, pendingKey=None,
                consumed=0, lastSlot=None,
                controllerId=f"{owner}:schedule-{identifier}-{generation}",
                history=[], createdAt=before.value.createdAt if before else before_owner.now,
                updatedAt=before_owner.now,
            )
            if await self.store.write_schedule(before_owner, updated_owner, value, before):
                return await self.ensure_started(value)
        raise AutomationError("schedule_changed", "Schedule coordination is contended.", status=503)

    async def ensure_started(self, value: WorkflowSchedule) -> WorkflowSchedule:
        if not value.enabled or value.status not in {"pending", "acceptance_unknown"}:
            return value
        host = self.automation.host
        if host is None:
            raise AutomationError("automation_unavailable", "The durable host is unavailable.", status=503)
        status = "active"
        reason = None
        try:
            await host.start_schedule(value.userId, value.scheduleId, value.generation, value.controllerId)
        except DurableScheduleAcceptanceUnknownError:
            status, reason = "acceptance_unknown", "scheduler_ack_unknown"
        except DurableScheduleRejectedError:
            status, reason = "paused", "scheduler_rejected"
        for _ in range(3):
            owner = await self.automation.owner(value.userId)
            current = await self.store.read_schedule(value.userId, value.scheduleId)
            if current is None:
                raise AutomationError("schedule_missing", "The schedule is unavailable.", status=503)
            if (
                current.value.generation != value.generation
                or current.value.status not in {"pending", "acceptance_unknown"}
            ):
                return current.value
            updated = current.value.model_copy(update={
                "status": status, "reason": reason, "revision": current.value.revision + 1,
            })
            updated_owner = owner.value.model_copy(update={"revision": owner.value.revision + 1}, deep=True)
            if await self.store.write_schedule(owner, updated_owner, updated, current):
                return updated
        raise AutomationError("schedule_changed", "Schedule acknowledgement could not be recorded.", status=503)

    async def disable(self, owner_id: str, schedule_id: str, revision: int) -> WorkflowSchedule:
        for _ in range(3):
            owner = await self.automation.owner(owner_id)
            prior = await self.store.read_schedule(owner_id, schedule_id)
            if prior is None:
                raise AutomationError("schedule_missing", "The schedule is unavailable.", status=404)
            if prior.value.revision != revision:
                raise AutomationError("schedule_changed", "Reload the schedule before disabling it.")
            updated = prior.value.model_copy(update={
                "enabled": False, "status": "disabled", "reason": "owner_disabled",
                "revision": prior.value.revision + 1, "updatedAt": owner.now,
            }, deep=True)
            updated_owner = owner.value.model_copy(update={"revision": owner.value.revision + 1}, deep=True)
            if await self.store.write_schedule(owner, updated_owner, updated, prior):
                return updated
        raise AutomationError("schedule_changed", "Schedule coordination is contended.", status=503)

    async def pause(self, owner_id: str, schedule_id: str, generation: int, reason: str) -> None:
        for _ in range(3):
            owner = await self.automation.owner(owner_id)
            prior = await self.store.read_schedule(owner_id, schedule_id)
            if prior is None:
                raise AutomationError("schedule_missing", "The schedule is unavailable.", status=503)
            if prior.value.generation != generation or not prior.value.enabled:
                return
            updated = prior.value.model_copy(update={
                "status": "paused", "reason": reason, "revision": prior.value.revision + 1,
            }, deep=True)
            updated_owner = owner.value.model_copy(update={"revision": owner.value.revision + 1}, deep=True)
            if await self.store.write_schedule(owner, updated_owner, updated, prior):
                return
        raise AutomationError("schedule_changed", "Schedule coordination is contended.", status=503)

    @staticmethod
    def next_slot(schedule: WorkflowSchedule, now: datetime) -> None:
        slot = schedule.pendingSlot or schedule.next
        if slot is None:
            raise AutomationError("schedule_corrupt", "A schedule has no claimed occurrence.")
        schedule.consumed += 1
        schedule.lastSlot = slot.localSlot
        schedule.next = next_occurrence(
            schedule.rule, slot.dueAt, after_slot=slot.localSlot,
            expected_zone_digest=slot.zoneDigest,
        ) if schedule.consumed < schedule.rule.maxOccurrences else None
        schedule.pendingSlot = None
        schedule.pendingKey = None
        schedule.history = schedule.history[-MAX_SCHEDULE_HISTORY:]
        if schedule.next is None:
            schedule.status = "completed"
            schedule.enabled = False
        schedule.updatedAt = now

    async def tick(self, owner_id: str, schedule_id: str, generation: int) -> dict[str, Any]:
        try:
            self.automation.require_enabled(scheduling=True)
        except AutomationError as exc:
            await self.pause(owner_id, schedule_id, generation, exc.code)
            return {"terminal": True, "status": "paused"}
        for _ in range(3):
            owner = await self.automation.owner(owner_id)
            prior = await self.store.read_schedule(owner_id, schedule_id)
            if prior is None:
                raise AutomationError("schedule_missing", "The schedule is unavailable.", status=503)
            value = prior.value
            if value.generation != generation or not value.enabled or value.status in {"paused", "completed", "disabled"}:
                return {"terminal": True, "status": value.status}
            if value.next is None:
                raise AutomationError("schedule_corrupt", "An active schedule has no next occurrence.")
            _, identity = zone_identity(value.rule.timezone)
            if identity != value.next.zoneDigest:
                await self.pause(owner_id, schedule_id, generation, "timezone_changed")
                return {"terminal": True, "status": "paused"}
            if value.pendingSlot is None and owner.now < value.next.dueAt:
                return {"terminal": False, "waitUntil": value.next.dueAt.isoformat()}
            if value.pendingSlot is None:
                updated = value.model_copy(deep=True)
                if owner.now > value.next.dueAt + timedelta(seconds=MISSED_GRACE_SECONDS):
                    for _missed in range(value.rule.maxOccurrences - value.consumed):
                        if updated.next is None or owner.now <= updated.next.dueAt + timedelta(seconds=MISSED_GRACE_SECONDS):
                            break
                        updated.history.append(ScheduleHistory(
                            slot=updated.next.localSlot, dueAt=updated.next.dueAt, outcome="missed", runId=None,
                        ))
                        self.next_slot(updated, owner.now)
                else:
                    updated.pendingSlot = value.next
                    updated.pendingKey = value.next.dueAt.isoformat().replace("+00:00", "Z") + "~" + stable_id(
                        owner_id, schedule_id, str(generation), value.next.localSlot, value.next.dueAt.isoformat(),
                    )[:32]
                updated.revision += 1
                updated.status = "active" if updated.enabled else "completed"
                updated_owner = owner.value.model_copy(update={"revision": owner.value.revision + 1}, deep=True)
                if not await self.store.write_schedule(owner, updated_owner, updated, prior):
                    continue
                if updated.pendingSlot is None:
                    return {"terminal": not updated.enabled, "waitUntil": updated.next.dueAt.isoformat() if updated.next else None}
                value = updated
            if value.pendingSlot is None or value.pendingKey is None:
                raise AutomationError("schedule_corrupt", "The occurrence claim is incomplete.")
            outcome, run_id = "launched", None
            try:
                with constrain_request(tools=value.allowTools, automatic_memory=value.allowAutomaticMemory):
                    run = await self.automation.start(
                        value.bundle, value.input, value.limits, value.pendingKey, user=None,
                        schedule_id=schedule_id, schedule_generation=generation,
                    )
                run_id = run.runId
                if run.status == "acceptance_unknown":
                    return {"terminal": False, "waitUntil": (owner.now + timedelta(seconds=30)).isoformat()}
            except AutomationError as exc:
                if exc.code == "overlap_denied":
                    outcome = "overlap"
                elif exc.code == "occurrence_missed":
                    outcome = "missed"
                else:
                    await self.pause(owner_id, schedule_id, generation, exc.code)
                    return {"terminal": True, "status": "paused"}
            for _finish in range(3):
                current_owner = await self.automation.owner(owner_id)
                current = await self.store.read_schedule(owner_id, schedule_id)
                if current is None:
                    raise AutomationError("schedule_missing", "The schedule is unavailable.", status=503)
                if current.value.generation != generation or current.value.pendingKey != value.pendingKey:
                    return {"terminal": current.value.generation != generation, "status": current.value.status}
                updated = current.value.model_copy(deep=True)
                updated.history.append(ScheduleHistory(
                    slot=value.pendingSlot.localSlot, dueAt=value.pendingSlot.dueAt,
                    outcome=outcome, runId=run_id,
                ))
                self.next_slot(updated, current_owner.now)
                updated.revision += 1
                new_owner = current_owner.value.model_copy(update={"revision": current_owner.value.revision + 1}, deep=True)
                if await self.store.write_schedule(current_owner, new_owner, updated, current):
                    return {"terminal": not updated.enabled, "waitUntil": updated.next.dueAt.isoformat() if updated.next else None}
            raise AutomationError("schedule_changed", "The occurrence outcome could not be recorded.", status=503)
        raise AutomationError("schedule_changed", "The occurrence could not be claimed.", status=503)
