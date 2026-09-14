"""Per-run money exercises the same owner CAS used by shipping workflow effects."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ai4ia_api.hard_quota.coverage import AttemptEnvelope, reservation_bounds
from ai4ia_api.usage.models import UsageRecord
from ai4ia_api.workflows.automation_common import AutomationError, MAX_STATE_BYTES, digest, json_bytes
from ai4ia_api.workflows.automation_models import AutomationOwner, EffectIntent, RunHandle, persisted_model, writable_body
from ai4ia_api.workflows.automation_service import WorkflowAutomationService
from ai4ia_api.workflows.automation_store import CosmosAutomationStore, InMemoryAutomationStore
from ai4ia_api.workflows.monetary_ledger import compact_money, monetary_transition_bytes, reserve_money, settle_money
from ai4ia_api.workflows.monetary_models import RunMoney, budget_identity
from tests.cosmos_deletion_fake import Container
from tests.test_hard_quota_dispatch import DEPLOYMENT, Harness
from tests.test_workflow_owner_store import owner

RUN = "owner:run"
NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def bounds():
    h = Harness()
    return reservation_bounds(
        "chat", {"messages": [{"role": "user", "content": "hello"}]},
        deployment=DEPLOYMENT, catalog=h.catalog, pricing=h.pricing,
        attempts=AttemptEnvelope("fixture-one-send-v1", 1),
    )


def funded(store, limit, *, legacy=False):
    value = owner(store)
    account = None if legacy else RunMoney.new(
        budget_identity("owner", value.epoch, RUN, "a" * 64, limit), limit,
    )
    value.runs[RUN] = RunHandle(
        runId=RUN, sessionId="session", checkpointId="checkpoint", fingerprint="a" * 64,
        workflowKey="flow", idempotencyKey="2026-09-14T00:00:00Z~" + "1" * 32,
        createdAt=NOW, active=True, terminal=False, modelCalls=0, toolCalls=0,
        dispatches=0, operationFloor=-1, scheduleId=None, scheduleGeneration=None, money=account,
    )
    return value


def reserve(value, identifier="one", *, payload="original"):
    if identifier in value.effects:
        raise AutomationError("operation_replayed", "The dispatch cannot be repeated.")
    value.effects[identifier] = EffectIntent(
        id=identifier, runId=RUN, sessionId="session", operationId=f"operation-{identifier}",
        category="dispatch", payloadDigest=digest(payload), state="dispatched", startedAt=NOW,
        resultDigest=None, delivered=False,
        usage=UsageRecord(
            id="usage-" + identifier, userId="owner", sessionId="session", model="fixture",
            createdAt=NOW, workflowDispatchClaimed=True,
        ),
        money=reserve_money(value, RUN, bounds()),
    )
    value.runs[RUN].dispatches += 1


def settle(value, identifier="one", *, completed=True, usage=None, outcome="complete"):
    effect = value.effects[identifier]
    settle_money(value, effect, completed=completed, usage=usage, outcome=outcome)
    effect.state = "complete" if effect.money.phase == "settled" else "unknown"
    effect.resultDigest = digest({"completed": completed, "usage": usage, "outcome": outcome})


@pytest.fixture(params=["memory", "cosmos"])
def store(request):
    return InMemoryAutomationStore() if request.param == "memory" else CosmosAutomationStore(Container("userId"))


@pytest.mark.parametrize("limit,allowed", [(139, False), (140, True), (141, True)])
async def test_exact_below_at_and_above_budget_reserve_before_dispatch(store, limit, allowed):
    await store.create_owner(funded(store, limit))
    service = WorkflowAutomationService(None, store, None)
    sent = []
    try:
        await service.mutate_owner("owner", lambda value, now: reserve(value))
        snapshot = await store.read_owner("owner")
        assert snapshot.value.runs[RUN].money.heldMicroUsd == 140
        sent.append("provider")
    except AutomationError as exc:
        assert exc.code == "spend_limit"
        assert not allowed
    assert sent == (["provider"] if allowed else [])
    snapshot = await store.read_owner("owner")
    assert snapshot.value.runs[RUN].money.heldMicroUsd == (140 if allowed else 0)
    assert len(snapshot.value.effects) == int(allowed)


@pytest.mark.parametrize("limit,expected", [(279, 1), (280, 2)])
async def test_simultaneous_dispatches_compete_on_actual_owner_etag(store, limit, expected):
    await store.create_owner(funded(store, limit))
    original = store.write_owner
    entered = 0
    both = asyncio.Event()
    observed_etags = []

    async def race(prior, updated):
        nonlocal entered
        if entered < 2:
            entered += 1
            observed_etags.append(prior.etag)
            if entered == 2:
                both.set()
            await both.wait()
        return await original(prior, updated)

    store.write_owner = race
    service = WorkflowAutomationService(None, store, None)

    async def dispatch(identifier):
        try:
            await service.mutate_owner("owner", lambda value, now: reserve(value, identifier))
        except AutomationError as exc:
            assert exc.code == "spend_limit"
            return False
        return True

    dispatched = await asyncio.gather(dispatch("one"), dispatch("two"))
    assert observed_etags[0] == observed_etags[1]
    assert sum(dispatched) == expected
    snapshot = await store.read_owner("owner")
    assert snapshot.value.runs[RUN].money.heldMicroUsd == 140 * expected
    assert len(snapshot.value.effects) == expected


@pytest.mark.parametrize(
    "completed,usage,outcome,charged",
    [
        (True, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, "complete", 20),
        (True, None, "complete", 140),
        (True, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 99}, "complete", 140),
        (False, {"prompt_tokens": 10, "completion_tokens": 5}, "cancelled", 140),
        (False, None, "timeout", 140),
    ],
)
async def test_only_complete_proven_usage_releases_unused_hold(store, completed, usage, outcome, charged):
    await store.create_owner(funded(store, 160))
    service = WorkflowAutomationService(None, store, None)
    await service.mutate_owner("owner", lambda value, now: reserve(value))
    await service.mutate_owner("owner", lambda value, now: settle(
        value, completed=completed, usage=usage, outcome=outcome,
    ))
    snapshot = await store.read_owner("owner")
    account = snapshot.value.runs[RUN].money
    assert account.settledMicroUsd + account.heldMicroUsd == charged
    assert account.unknownMicroUsd == (0 if charged == 20 else 140)
    if charged == 20:
        await service.mutate_owner("owner", lambda value, now: reserve(value, "two"))
    else:
        with pytest.raises(AutomationError, match="remaining"):
            await service.mutate_owner("owner", lambda value, now: reserve(value, "two"))
        later = snapshot.value.model_copy(deep=True)
        WorkflowAutomationService.compact(later, NOW + timedelta(days=365))
        assert later.runs[RUN].money == account
        assert "one" in later.effects


async def test_lost_reservation_and_settlement_ack_never_double_charge_or_dispatch(store):
    await store.create_owner(funded(store, 280))
    service = WorkflowAutomationService(None, store, None)
    write = store.write_owner
    lose = True

    async def lost_response(prior, updated):
        nonlocal lose
        result = await write(prior, updated)
        if result and lose:
            lose = False
            raise OSError("synthetic lost storage acknowledgment")
        return result

    store.write_owner = lost_response
    with pytest.raises(OSError, match="acknowledgment"):
        await service.mutate_owner("owner", lambda value, now: reserve(value))
    with pytest.raises(AutomationError, match="repeated"):
        await service.mutate_owner("owner", lambda value, now: reserve(value))
    with pytest.raises(AutomationError, match="repeated"):
        await service.mutate_owner("owner", lambda value, now: reserve(value, payload="changed"))
    assert (await store.read_owner("owner")).value.runs[RUN].money.heldMicroUsd == 140
    usage = {"prompt_tokens": 10, "completion_tokens": 5}
    lose = True
    with pytest.raises(OSError, match="acknowledgment"):
        await service.mutate_owner("owner", lambda value, now: settle(value, usage=usage))
    before = (await store.read_owner("owner")).value.runs[RUN].money
    await service.mutate_owner("owner", lambda value, now: settle(value, usage=usage))
    assert (await store.read_owner("owner")).value.runs[RUN].money == before
    with pytest.raises(AutomationError, match="differently"):
        await service.mutate_owner("owner", lambda value, now: settle(
            value, usage={"prompt_tokens": 1, "completion_tokens": 1},
        ))


async def test_compaction_retains_consumed_money_and_existing_operation_floor(store):
    await store.create_owner(funded(store, 159))
    service = WorkflowAutomationService(None, store, None)
    await service.mutate_owner("owner", lambda value, now: reserve(value))
    await service.mutate_owner("owner", lambda value, now: settle(
        value, usage={"prompt_tokens": 10, "completion_tokens": 5},
    ))
    await service.mutate_owner("owner", lambda value, now: setattr(value.effects["one"], "delivered", True))

    def prune(value, now):
        value.runs[RUN].operationFloor = 1
        compact_money(value, value.effects["one"])
        del value.effects["one"]

    await service.mutate_owner("owner", prune)
    current = (await store.read_owner("owner")).value
    assert current.effects == {}
    assert current.runs[RUN].money.compactedMicroUsd == current.runs[RUN].money.settledMicroUsd == 20
    assert current.runs[RUN].money.compactedReservations == 1
    with pytest.raises(AutomationError, match="remaining"):
        await service.mutate_owner("owner", lambda value, now: reserve(value, "two"))


@pytest.mark.parametrize("unknown", [False, True])
async def test_retirement_uses_the_same_key_horizon_for_selection_and_validation(store, unknown):
    value = funded(store, 140)
    value.runs[RUN].createdAt = NOW + timedelta(minutes=1)
    reserve(value)
    settle(
        value, completed=not unknown,
        usage=None if unknown else {"prompt_tokens": 10, "completion_tokens": 5},
        outcome="unknown" if unknown else "complete",
    )
    value.effects["one"].delivered = True
    value.runs[RUN].terminal = True
    value.runs[RUN].active = False
    if not unknown:
        compact_money(value, value.effects["one"])
        del value.effects["one"]
    await store.create_owner(value)
    observed = NOW + timedelta(days=30, seconds=30)
    if isinstance(store, CosmosAutomationStore):
        store._container.now = observed
    else:
        store.clock = lambda: observed
    service = WorkflowAutomationService(None, store, None)

    def admit(other, now):
        service.compact(other, now)
        other.runs["owner:next"] = value.runs[RUN].model_copy(update={
            "runId": "owner:next", "sessionId": "next", "checkpointId": "next",
            "idempotencyKey": now.isoformat().replace("+00:00", "Z") + "~" + "2" * 32,
            "createdAt": now, "active": True, "terminal": False, "money": None,
        }, deep=True)

    await service.mutate_owner("owner", admit)
    latest = (await store.read_owner("owner")).value
    assert "owner:next" in latest.runs
    assert (RUN in latest.runs) is unknown
    if unknown:
        assert latest.runs[RUN].money == value.runs[RUN].money
        assert latest.runs[RUN].money.unknownMicroUsd == 140


async def test_changed_limit_price_payload_or_removed_unknown_is_not_a_valid_transition(store):
    await store.create_owner(funded(store, 280))
    service = WorkflowAutomationService(None, store, None)
    await service.mutate_owner("owner", lambda value, now: reserve(value))
    prior = await store.read_owner("owner")
    for field, changed in (
        ("limitMicroUsd", 281), ("budgetId", "f" * 64), ("settledMicroUsd", 1),
    ):
        updated = prior.value.model_copy(deep=True)
        updated.revision += 1
        updated.runs[RUN].money = updated.runs[RUN].money.model_copy(update={field: changed})
        with pytest.raises((AutomationError, ValueError)):
            await store.write_owner(prior, updated)
    for kind in ("price", "payload", "drop"):
        updated = prior.value.model_copy(deep=True)
        updated.revision += 1
        effect = updated.effects["one"]
        if kind == "price":
            effect.money = effect.money.model_copy(update={
                "bounds": effect.money.bounds.model_copy(update={"priceVersion": "different"}),
            })
        elif kind == "payload":
            effect.payloadDigest = "f" * 64
        else:
            del updated.effects["one"]
        with pytest.raises((AutomationError, ValueError)):
            await store.write_owner(prior, updated)
    assert (await store.read_owner("owner")).value == prior.value


async def test_settlement_survives_terminal_run_without_execution_authority(store):
    await store.create_owner(funded(store, 280))
    service = WorkflowAutomationService(None, store, None)
    await service.mutate_owner("owner", lambda value, now: reserve(value))
    await service.mutate_owner("owner", lambda value, now: setattr(value.runs[RUN], "terminal", True))
    await service.mutate_owner("owner", lambda value, now: settle(
        value, usage={"prompt_tokens": 10, "completion_tokens": 5},
    ))
    snapshot = (await store.read_owner("owner")).value
    assert snapshot.runs[RUN].terminal
    assert snapshot.runs[RUN].money.settledMicroUsd == 20
    with pytest.raises(AutomationError, match="no longer"):
        await service.mutate_owner("owner", lambda value, now: reserve(value, "two"))


@pytest.mark.parametrize("unknown", [False, True])
async def test_consistent_zero_charge_is_allowed_only_for_complete_proven_usage(store, unknown):
    await store.create_owner(funded(store, 140))
    service = WorkflowAutomationService(None, store, None)
    await service.mutate_owner("owner", lambda value, now: reserve(value))
    await service.mutate_owner("owner", lambda value, now: settle(
        value, completed=not unknown, usage={"prompt_tokens": 0, "completion_tokens": 0},
        outcome="timeout" if unknown else "complete",
    ))
    if unknown:
        prior = await store.read_owner("owner")
        updated = prior.value.model_copy(deep=True)
        updated.revision += 1
        account = updated.runs[RUN].money
        updated.runs[RUN].money = account.model_copy(update={
            "heldMicroUsd": 0, "unknownMicroUsd": 0, "revision": account.revision + 1,
        })
        effect = updated.effects["one"]
        effect.money = effect.money.model_copy(update={
            "phase": "settled", "chargedMicroUsd": 0, "settlementDigest": digest("invented complete"),
        })
        effect.state = "complete"
        # The arithmetic and persisted shape are internally coherent. Only the
        # immutable unknown transition prevents this apparent refund.
        writable_body(updated)
        with pytest.raises(AutomationError, match="cannot change"):
            await store.write_owner(prior, updated)
        assert (await store.read_owner("owner")).value == prior.value
    else:
        current = (await store.read_owner("owner")).value
        assert current.runs[RUN].money.heldMicroUsd == current.runs[RUN].money.settledMicroUsd == 0
        assert current.effects["one"].money.phase == "settled"
        await service.mutate_owner("owner", lambda value, now: reserve(value, "two"))


async def test_bound_violation_retains_observed_charge_and_blocks_further_work(store):
    await store.create_owner(funded(store, 1000))
    service = WorkflowAutomationService(None, store, None)
    await service.mutate_owner("owner", lambda value, now: reserve(value))
    await service.mutate_owner("owner", lambda value, now: settle(
        value, usage={"prompt_tokens": 500, "completion_tokens": 500},
    ))
    account = (await store.read_owner("owner")).value.runs[RUN].money
    assert account.settledMicroUsd == 1500
    assert account.blocked and account.reason == "bound_exceeded"
    assert account.remaining_micro_usd == 0
    with pytest.raises(AutomationError, match="no longer"):
        await service.mutate_owner("owner", lambda value, now: reserve(value, "two"))


def test_legacy_owner_shape_is_preserved_but_money_record_cannot_default_missing_fields():
    store = InMemoryAutomationStore()
    legacy = funded(store, 0, legacy=True)
    raw = legacy.model_dump(mode="json")
    assert "money" not in raw["runs"][RUN]
    assert writable_body(persisted_model(AutomationOwner, raw)) == raw
    capped = funded(store, 280)
    reserve(capped)
    for path in ("account", "dispatch", "price"):
        raw = capped.model_dump(mode="json")
        if path == "account":
            del raw["runs"][RUN]["money"]["heldMicroUsd"]
        elif path == "dispatch":
            del raw["effects"]["one"]["money"]["settlementDigest"]
        else:
            del raw["effects"]["one"]["money"]["bounds"]["inputRate"]
        with pytest.raises((AutomationError, ValueError)):
            persisted_model(AutomationOwner, raw)


def test_every_outstanding_monetary_transition_reserves_escaped_wire_space():
    value = funded(InMemoryAutomationStore(), 100_000)
    for index in range(20):
        reserve(value, str(index))
    body = writable_body(value)
    extra = monetary_transition_bytes(body)
    assert extra > 20 * 60
    assert len(json_bytes(body)) + extra + 32768 + 20 * 8192 < MAX_STATE_BYTES
    mutated = value.model_dump(mode="json")
    del mutated["runs"][RUN]["money"]
    with pytest.raises((AutomationError, ValueError)):
        persisted_model(AutomationOwner, mutated)
