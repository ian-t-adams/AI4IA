import asyncio
from datetime import datetime, timezone

import pytest

from ai4ia_api.usage.models import UsageRecord
from ai4ia_api.workflows.automation_common import AutomationError, MAX_STATE_BYTES, json_bytes
from ai4ia_api.workflows.automation_models import (
    AutomationOwner, EffectIntent, RunHandle, writable_body,
)
from ai4ia_api.workflows.automation_store import CosmosAutomationStore, InMemoryAutomationStore
from tests.cosmos_deletion_fake import Container


def owner(store):
    return AutomationOwner(
        id=store.owner_id, userId="owner", recordKind=store.owner_kind, epoch="epoch",
        revision=0, requestFloor=datetime(1970, 1, 1, tzinfo=timezone.utc),
        runs={}, schedules=[], effects={},
    )


@pytest.fixture(params=["memory", "cosmos"])
def store(request):
    return InMemoryAutomationStore() if request.param == "memory" else CosmosAutomationStore(Container("userId"))


async def test_owner_create_and_conditional_replace_are_single_winner(store):
    value = owner(store)
    assert await store.create_owner(value)
    assert not await store.create_owner(value)
    before = await store.read_owner("owner")
    assert before is not None
    updated = before.value.model_copy(update={"revision": 1}, deep=True)
    results = await asyncio.gather(store.write_owner(before, updated), store.write_owner(before, updated))
    assert sorted(results) == [False, True]
    assert await store.read_owner("other") is None


async def test_missing_or_mistyped_persisted_coordination_is_not_an_empty_owner():
    container = Container("userId")
    store = CosmosAutomationStore(container)
    raw = owner(store).model_dump(mode="json")
    del raw["effects"]
    await container.create_item(raw)
    with pytest.raises(AutomationError, match="Required"):
        await store.read_owner("owner")


def test_all_outstanding_dispatches_keep_room_for_terminal_accounting():
    value = owner(InMemoryAutomationStore())
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    run_id = "owner:run"
    value.runs[run_id] = RunHandle(
        runId=run_id, sessionId="session", checkpointId="checkpoint", fingerprint="a" * 64,
        workflowKey="flow", idempotencyKey="2026-09-10T00:00:00Z~" + "1" * 32,
        createdAt=now, active=True, terminal=False, modelCalls=0, toolCalls=0,
        dispatches=0, operationFloor=-1, scheduleId=None, scheduleGeneration=None,
    )
    allowed = 0
    for index in range(80):
        key = f"effect-{index}"
        value.effects[key] = EffectIntent(
            id=key, runId=run_id, sessionId="session", operationId="operation",
            category="dispatch", payloadDigest="b" * 64, state="dispatched",
            startedAt=now, resultDigest=None, delivered=False,
            usage=UsageRecord(
                id=key, userId="owner", sessionId="session", model="model",
                status="error", workflowDispatchClaimed=True, createdAt=now,
            ),
        )
        try:
            body = writable_body(value)
        except AutomationError as exc:
            assert exc.code == "state_limit"
            break
        allowed += 1
        assert len(json_bytes(body)) + 32768 + 8192 * len(value.effects) <= MAX_STATE_BYTES
    else:
        pytest.fail("Outstanding dispatch transition space was not reserved.")
    assert allowed > 0
