from datetime import datetime, timezone
import json
import logging

import pytest
from durabletask import task
from durabletask.serialization import JsonDataConverter
from durabletask.worker import _OrchestrationExecutor, _Registry, _RuntimeOrchestrationContext
from durabletask.internal import orchestrator_service_pb2 as protocol
from google.protobuf.timestamp_pb2 import Timestamp
from google.protobuf.wrappers_pb2 import StringValue

from ai4ia_api.workflows.durable_automation import run_orchestrator, schedule_orchestrator


def _stamp(seconds=1789063200):
    return Timestamp(seconds=seconds)


def _execute(registry, old, new):
    # Serialized event round trips model a different worker, not a retained
    # Python generator or an arbitrary counter standing in for SDK history.
    restored = [protocol.HistoryEvent.FromString(event.SerializeToString()) for event in old]
    incoming = [protocol.HistoryEvent.FromString(event.SerializeToString()) for event in new]
    return _OrchestrationExecutor(
        registry, logging.getLogger("workflow-sdk-fixture"), JsonDataConverter(),
    ).execute("owner:run", restored, incoming)


def _started():
    registry = _Registry()
    registry.add_orchestrator(run_orchestrator)
    reference = {"ownerId": "owner", "runId": "owner:run", "deadline": "2026-09-10T18:30:00+00:00"}
    history = [
        protocol.HistoryEvent(eventId=-1, timestamp=_stamp(), orchestratorStarted=protocol.OrchestratorStartedEvent()),
        protocol.HistoryEvent(eventId=0, timestamp=_stamp(), executionStarted=protocol.ExecutionStartedEvent(
            name=run_orchestrator.__name__, input=StringValue(value=json.dumps(reference)),
            orchestrationInstance=protocol.OrchestrationInstance(instanceId="owner:run"),
        )),
    ]
    actions = _execute(registry, [], history).actions
    for action in actions:
        if action.HasField("createTimer"):
            history.append(protocol.HistoryEvent(
                eventId=action.id, timestamp=_stamp(),
                timerCreated=protocol.TimerCreatedEvent(fireAt=action.createTimer.fireAt),
            ))
        else:
            history.append(protocol.HistoryEvent(
                eventId=action.id, timestamp=_stamp(), taskScheduled=protocol.TaskScheduledEvent(
                    name=action.scheduleTask.name, input=action.scheduleTask.input,
                ),
            ))
    history.append(protocol.HistoryEvent(
        eventId=-1, timestamp=_stamp(), orchestratorCompleted=protocol.OrchestratorCompletedEvent(),
    ))
    return registry, history


@pytest.mark.parametrize("orchestrator", [run_orchestrator, schedule_orchestrator])
@pytest.mark.parametrize("failed_activity", [False, True])
def test_real_sdk_naive_utc_context_suspends_or_retries_without_dying(orchestrator, failed_activity):
    stamp = Timestamp()
    stamp.FromDatetime(datetime(2026, 9, 10, 18, tzinfo=timezone.utc))
    reference = {
        "ownerId": "owner", "runId": "owner:run", "scheduleId": "schedule", "generation": 1,
        "deadline": "2026-09-10T18:30:00+00:00",
    }
    context = _RuntimeOrchestrationContext("owner:run", _Registry(), JsonDataConverter())
    context.current_utc_datetime = stamp.ToDatetime()
    assert context.current_utc_datetime.tzinfo is None
    context.run(orchestrator(context, reference))
    activity = next(
        value for value in context._pending_tasks.values()
        if not isinstance(value, task.TimerTask)
    )
    if failed_activity:
        activity.fail("Coordination unavailable", ConnectionError("synthetic outage"))
    else:
        activity.complete({
            "terminal": False, "status": "awaiting_approval",
            "waitUntil": "2026-09-10T18:10:00+00:00",
        })
    context.resume()
    assert any(
        isinstance(value, task.TimerTask) and not value.is_complete
        for value in context._pending_tasks.values()
    )


def test_coordination_failure_retries_inside_the_same_sdk_context_and_can_complete():
    context = _RuntimeOrchestrationContext("owner:run", _Registry(), JsonDataConverter())
    context.current_utc_datetime = datetime(2026, 9, 10, 18)
    reference = {
        "ownerId": "owner", "runId": "owner:run",
        "deadline": "2026-09-10T18:30:00+00:00",
    }
    context.run(run_orchestrator(context, reference))
    first = next(value for value in context._pending_tasks.values() if not isinstance(value, task.TimerTask))
    first.fail("Coordination unavailable", ConnectionError("synthetic outage"))
    context.resume()
    timers = [value for value in context._pending_tasks.values() if isinstance(value, task.TimerTask)]
    assert len(timers) == 2
    context.current_utc_datetime = datetime(2026, 9, 10, 18, 0, 30)
    timers[-1].complete(None)
    context.resume()
    retry = next(
        value for value in context._pending_tasks.values()
        if not isinstance(value, task.TimerTask) and not value.is_complete
    )
    assert retry is not first
    retry.complete({"terminal": True, "status": "completed"})
    with pytest.raises(StopIteration) as completed:
        context.resume()
    assert completed.value.value == {"terminal": True, "status": "completed"}
    assert all(value.is_complete for value in timers)


@pytest.mark.parametrize("failed", [False, True])
def test_serialized_sdk_history_replays_wait_or_failed_activity_without_rescheduling_completed_work(failed):
    registry, history = _started()
    new = [protocol.HistoryEvent(
        eventId=-1, timestamp=_stamp(), orchestratorStarted=protocol.OrchestratorStartedEvent(),
    )]
    if failed:
        new.append(protocol.HistoryEvent(
            eventId=3, timestamp=_stamp(), taskFailed=protocol.TaskFailedEvent(
                taskScheduledId=2, failureDetails=protocol.TaskFailureDetails(
                    errorType="ConnectionError", errorMessage="Synthetic coordination outage",
                ),
            ),
        ))
    else:
        new.append(protocol.HistoryEvent(
            eventId=3, timestamp=_stamp(), taskCompleted=protocol.TaskCompletedEvent(
                taskScheduledId=2, result=StringValue(value=json.dumps({
                    "terminal": False, "status": "awaiting_approval",
                    "waitUntil": "2026-09-10T18:10:00+00:00",
                })),
            ),
        ))
    first = _execute(registry, history, new)
    replay = _execute(registry, history, new)
    assert [action.SerializeToString() for action in first.actions] == [
        action.SerializeToString() for action in replay.actions
    ]
    assert len(first.actions) == 1
    assert first.actions[0].HasField("createTimer")
    assert first.actions[0].createTimer.fireAt.seconds == 1789063230
