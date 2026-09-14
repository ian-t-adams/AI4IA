from datetime import datetime, timedelta, timezone
from functools import partial
import pytest

from ai4ia_api.workflows.automation_access import WorkflowSelection
from ai4ia_api.workflows.automation_common import ExecutionLimits
from ai4ia_api.workflows.schedule_service import WorkflowScheduleService
from ai4ia_api.workflows.scheduling import ScheduleRule
from tests.test_workflow_automation_service import begin, install


def prepare(client, *, daily=False):
    automation, calls, _ = install(client)
    user, run = begin(client, automation)
    client.portal.call(automation.advance, user.internal_user_id, run.runId)
    calls.clear()
    service = WorkflowScheduleService(automation)
    now = datetime.now(timezone.utc)
    due = (now + timedelta(minutes=2)).replace(second=0, microsecond=0)
    clock = {"now": now}
    automation.store.clock = lambda: clock["now"]
    rule = ScheduleRule(
        frequency="daily" if daily else "once", timezone="UTC",
        localTime=due.time().replace(tzinfo=None),
        localDate=None if daily else due.date(), maxOccurrences=2 if daily else 1,
    )
    key = now.isoformat().replace("+00:00", "Z") + "~" + "2" * 32
    saved = client.portal.call(partial(
        service.save, user, WorkflowSelection(name="flow", model="gpt-5.4"), "Scheduled calculation.",
        ExecutionLimits(spendMode="no_hard_dollar_cap"), rule, key,
    ))
    return automation, service, user, saved, clock, calls


def test_timer_claims_one_occurrence_and_never_duplicates_a_run(client):
    automation, schedules, user, saved, clock, calls = prepare(client)
    before = client.portal.call(schedules.tick, user.internal_user_id, saved.scheduleId, saved.generation)
    assert not before["terminal"] and len(automation.host.started) == 1
    clock["now"] = saved.next.dueAt
    first = client.portal.call(schedules.tick, user.internal_user_id, saved.scheduleId, saved.generation)
    assert first["terminal"]
    result = client.portal.call(schedules.list, user.internal_user_id)[0]
    assert result.consumed == 1 and result.history[0].outcome == "launched"
    assert calls == []
    repeat = client.portal.call(schedules.tick, user.internal_user_id, saved.scheduleId, saved.generation)
    assert repeat["terminal"] and len(automation.host.started) == 2
    client.portal.call(automation.advance, user.internal_user_id, result.history[0].runId)
    assert len(calls) == 2


def test_long_downtime_skips_without_backfill(client):
    automation, schedules, user, saved, clock, calls = prepare(client, daily=True)
    clock["now"] = saved.next.dueAt + timedelta(days=10)
    result = client.portal.call(schedules.tick, user.internal_user_id, saved.scheduleId, saved.generation)
    assert result["terminal"]
    saved = client.portal.call(schedules.list, user.internal_user_id)[0]
    assert saved.consumed == 2
    assert [item.outcome for item in saved.history] == ["missed", "missed"]
    assert len(automation.host.started) == 1 and calls == []


def test_overlap_does_not_release_an_unfinished_previous_occurrence(client):
    automation, schedules, user, saved, clock, calls = prepare(client, daily=True)
    clock["now"] = saved.next.dueAt
    client.portal.call(schedules.tick, user.internal_user_id, saved.scheduleId, saved.generation)
    current = client.portal.call(schedules.list, user.internal_user_id)[0]
    clock["now"] = current.next.dueAt
    client.portal.call(schedules.tick, user.internal_user_id, saved.scheduleId, saved.generation)
    current = client.portal.call(schedules.list, user.internal_user_id)[0]
    assert [item.outcome for item in current.history] == ["launched", "overlap"]
    assert len(automation.host.started) == 2 and calls == []


def test_disabled_schedule_cannot_launch_after_its_timer_fires(client):
    automation, schedules, user, saved, clock, calls = prepare(client)
    current = client.portal.call(schedules.list, user.internal_user_id)[0]
    client.portal.call(schedules.disable, user.internal_user_id, saved.scheduleId, current.revision)
    clock["now"] = saved.next.dueAt
    result = client.portal.call(schedules.tick, user.internal_user_id, saved.scheduleId, saved.generation)
    assert result["terminal"]
    assert len(automation.host.started) == 1 and calls == []


def test_unadmitted_pending_slot_still_obeys_missed_grace(client):
    automation, schedules, user, saved, clock, calls = prepare(client)
    clock["now"] = saved.next.dueAt
    original = automation.start

    async def interrupted(*args, **kwargs):
        raise ConnectionError("worker lost before run admission")

    automation.start = interrupted
    with pytest.raises(ConnectionError):
        client.portal.call(schedules.tick, user.internal_user_id, saved.scheduleId, saved.generation)
    pending = client.portal.call(schedules.list, user.internal_user_id)[0]
    assert pending.pendingSlot is not None
    automation.start = original
    clock["now"] += timedelta(hours=1)
    client.portal.call(schedules.tick, user.internal_user_id, saved.scheduleId, saved.generation)
    current = client.portal.call(schedules.list, user.internal_user_id)[0]
    assert current.history[0].outcome == "missed"
    assert len(automation.host.started) == 1 and not calls


def test_edit_retries_return_the_exact_committed_generation(client):
    automation, schedules, user, saved, _, _ = prepare(client, daily=True)
    key = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z") + "~" + "e" * 32
    write = partial(
        schedules.save, user, WorkflowSelection(name="flow", model="gpt-5.4"), "Revised input.",
        saved.limits, saved.rule, key, schedule_id=saved.scheduleId, expected_revision=saved.revision,
    )
    first = client.portal.call(write)
    assert first.generation == 2
    assert client.portal.call(write) == first
    assert client.portal.call(write) == first
    from ai4ia_api.workflows.automation_common import AutomationError

    with pytest.raises(AutomationError, match="different schedule"):
        client.portal.call(partial(
            schedules.save, user, WorkflowSelection(name="flow", model="gpt-5.4"), "Changed again.",
            saved.limits, saved.rule, key, schedule_id=saved.scheduleId, expected_revision=saved.revision,
        ))
