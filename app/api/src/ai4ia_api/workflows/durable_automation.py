"""Pure v3 orchestration control on the existing in-API Durable Task worker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .automation_models import WorkflowCheckpoint
from .automation_service import WorkflowAutomationService
from .durable import DurableWorkflowService
from .schedule_service import WorkflowScheduleService

RUN_ORCHESTRATOR = "ai4ia_workflow_run_v3"
SCHEDULE_ORCHESTRATOR = "ai4ia_workflow_schedule_v3"
ADVANCE_ACTIVITY = "ai4ia_workflow_advance_v3"
STOP_ACTIVITY = "ai4ia_workflow_stop_v3"
SCHEDULE_ACTIVITY = "ai4ia_workflow_tick_v3"
CONTROL_EVENT = "workflow_control_v3"


def _sdk_utc(value: datetime) -> datetime:
    """The DTS context's offset-free clock is explicitly UTC, not local time."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class DurableAutomationHost:
    def __init__(self, durable: DurableWorkflowService) -> None:
        self.durable = durable

    async def start_run(self, state: WorkflowCheckpoint) -> None:
        await self.durable.schedule(
            {
                "ownerId": state.userId, "runId": state.runId,
                "deadline": state.deadline.isoformat(),
            },
            user_id=state.userId, run_id=state.runId,
            orchestrator_name=RUN_ORCHESTRATOR, prevent_reuse=True,
        )

    async def wake(self, owner: str, run_id: str, revision: int) -> None:
        await self.durable.wake_automation(owner, run_id, revision)

    async def start_schedule(self, owner: str, schedule_id: str, generation: int, controller_id: str) -> None:
        await self.durable.schedule(
            {"ownerId": owner, "scheduleId": schedule_id, "generation": generation},
            user_id=owner, run_id=controller_id,
            orchestrator_name=SCHEDULE_ORCHESTRATOR, prevent_reuse=True,
        )


def run_orchestrator(ctx, reference):
    from durabletask import task

    deadline = _sdk_utc(datetime.fromisoformat(reference["deadline"]))
    deadline_task = ctx.create_timer(deadline)
    try:
        if reference.get("cleanupReason"):
            try:
                yield ctx.call_activity(STOP_ACTIVITY, input={
                    **reference, "reason": reference["cleanupReason"],
                })
                return {"status": "timed_out" if reference["cleanupReason"] == "runtime_exceeded" else "failed"}
            except task.TaskFailedError:
                yield ctx.create_timer(_sdk_utc(ctx.current_utc_datetime) + timedelta(seconds=60))
                ctx.continue_as_new(reference, save_events=False)
                return None
        for _ in range(256):
            advance = ctx.call_activity(ADVANCE_ACTIVITY, input=reference)
            try:
                winner = yield task.when_any([advance, deadline_task])
                if winner is not deadline_task:
                    result = yield advance
            except task.TaskFailedError:
                retry = ctx.create_timer(min(
                    deadline, _sdk_utc(ctx.current_utc_datetime) + timedelta(seconds=30),
                ))
                yield task.when_any([retry, deadline_task])
                retry.cancel()
                if _sdk_utc(ctx.current_utc_datetime) >= deadline:
                    ctx.continue_as_new({**reference, "cleanupReason": "runtime_exceeded"}, save_events=False)
                    return None
                continue
            if winner is deadline_task:
                ctx.continue_as_new({**reference, "cleanupReason": "runtime_exceeded"}, save_events=False)
                return None
            if result["terminal"]:
                return result
            when = result.get("waitUntil")
            if when is None:
                continue
            next_check = min(
                datetime.fromisoformat(when), deadline,
                _sdk_utc(ctx.current_utc_datetime) + timedelta(seconds=30),
            )
            event = ctx.wait_for_external_event(CONTROL_EVENT)
            reconciliation = ctx.create_timer(next_check)
            winner = yield task.when_any([event, reconciliation, deadline_task])
            event.cancel()
            reconciliation.cancel()
            if winner is deadline_task:
                ctx.continue_as_new({**reference, "cleanupReason": "runtime_exceeded"}, save_events=False)
                return None
        ctx.continue_as_new({**reference, "cleanupReason": "history_limit"}, save_events=False)
        return None
    finally:
        deadline_task.cancel()


run_orchestrator.__name__ = RUN_ORCHESTRATOR


def schedule_orchestrator(ctx, reference):
    from durabletask import task

    try:
        result = yield ctx.call_activity(SCHEDULE_ACTIVITY, input=reference)
    except task.TaskFailedError:
        yield ctx.create_timer(_sdk_utc(ctx.current_utc_datetime) + timedelta(seconds=30))
        ctx.continue_as_new(reference, save_events=False)
        return None
    if result["terminal"]:
        return result
    when = result.get("waitUntil")
    next_check = min(
        datetime.fromisoformat(when) if when else _sdk_utc(ctx.current_utc_datetime) + timedelta(seconds=30),
        _sdk_utc(ctx.current_utc_datetime) + timedelta(days=1),
    )
    timer = ctx.create_timer(next_check)
    event = ctx.wait_for_external_event(CONTROL_EVENT)
    yield task.when_any([timer, event])
    timer.cancel()
    event.cancel()
    ctx.continue_as_new(reference, save_events=False)


schedule_orchestrator.__name__ = SCHEDULE_ORCHESTRATOR


def register_automation(
    worker: Any, durable: DurableWorkflowService, automation: WorkflowAutomationService,
) -> DurableAutomationHost:
    schedules = WorkflowScheduleService(automation)

    def advance(_ctx, reference):
        return durable._run_on_app_loop(automation.advance(reference["ownerId"], reference["runId"]))

    def stop(_ctx, reference):
        reason = reference["reason"]
        result = durable._run_on_app_loop(automation.cancel(
            reference["ownerId"], reference["runId"],
            status="timed_out" if reason == "runtime_exceeded" else "failed", reason=reason,
        ))
        return result.status if result else "cancelled"

    def tick(_ctx, reference):
        return durable._run_on_app_loop(schedules.tick(
            reference["ownerId"], reference["scheduleId"], reference["generation"],
        ))

    advance.__name__ = ADVANCE_ACTIVITY
    stop.__name__ = STOP_ACTIVITY
    tick.__name__ = SCHEDULE_ACTIVITY
    worker.add_orchestrator(run_orchestrator)
    worker.add_orchestrator(schedule_orchestrator)
    for activity in (advance, stop, tick):
        worker.add_activity(activity)
    return DurableAutomationHost(durable)
