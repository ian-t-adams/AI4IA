"""One conformance suite across a local fake and real Cosmos adapter/fake ETags."""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, Mock

import pytest
from azure.core import MatchConditions
from azure.cosmos.exceptions import CosmosAccessConditionFailedError, CosmosResourceNotFoundError
from pydantic import ValidationError

from ai4ia_api.entitlements.models import DAY_SECONDS, MONTH_SECONDS, EntitlementLimits
from ai4ia_api.hard_quota.cosmos_store import CosmosReservationStore
from ai4ia_api.hard_quota.models import (
    MAX_ENTRIES,
    MAX_STATE_BYTES,
    STATE_ID,
    Amounts,
    Bounds,
    QuotaError,
    QuotaState,
    operation_id,
    state_document,
)
from ai4ia_api.hard_quota.service import ReservationService
from ai4ia_api.hard_quota.store import LocalReservationStore

NOW = 1_800_000_000


class CosmosDocument(dict):
    def __init__(self, body, now):
        super().__init__(body)
        self.now = now

    def get_response_headers(self):
        return {"date": format_datetime(datetime.fromtimestamp(self.now, timezone.utc))}


class StatefulContainer:
    """ETags depend on actual row state; no call-count-triggered conflicts."""

    def __init__(self, clock):
        self.clock = clock
        self.rows = {}
        self.layout = {"id": "usage", "partitionKey": {"paths": ["/userId"], "kind": "Hash"}}
        self.fail = False
        self.lose_ack_phase = None

    def seed(self, state):
        self.rows[(state.userId, state.id)] = {**state_document(state), "_etag": "1"}

    async def read(self):
        if self.fail:
            raise OSError("fake storage unavailable")
        return copy.deepcopy(self.layout)

    async def read_item(self, *, item, partition_key):
        if self.fail:
            raise OSError("fake storage unavailable")
        row = self.rows.get((partition_key, item))
        if row is None:
            raise CosmosResourceNotFoundError()
        # Yield AFTER taking the snapshot to force genuine stale-reader races.
        snapshot = CosmosDocument(copy.deepcopy(row), self.clock())
        await asyncio.sleep(0)
        return snapshot

    async def replace_item(self, *, item, body, etag=None, match_condition=None, **kwargs):
        if self.fail:
            raise OSError("fake storage unavailable")
        key = (body["userId"], item)
        current = self.rows.get(key)
        if current is None:
            raise CosmosResourceNotFoundError()
        if match_condition == MatchConditions.IfNotModified and etag != current["_etag"]:
            raise CosmosAccessConditionFailedError()
        updated = copy.deepcopy(body)
        updated["_etag"] = str(int(current["_etag"]) + 1)
        self.rows[key] = updated
        if self.lose_ack_phase and any(
            record["phase"] == self.lose_ack_phase for record in body["entries"].values()
        ):
            raise OSError("committed write lost its acknowledgement")
        return CosmosDocument(copy.deepcopy(updated), self.clock())


class RacingLocalStore(LocalReservationStore):
    async def read(self, owner):
        snapshot = await super().read(owner)
        await asyncio.sleep(0)
        return snapshot


@pytest.fixture(params=["local", "cosmos"])
def contract(request):
    clock = [NOW]
    seed = LocalReservationStore(clock=lambda: clock[0])
    alice = seed.seed("alice")
    bob = seed.seed("bob")
    if request.param == "local":
        store = RacingLocalStore(clock=lambda: clock[0])
        store._rows = copy.deepcopy(seed._rows)
        container = None
        account = None
    else:
        container = StatefulContainer(lambda: clock[0])
        container.seed(alice)
        container.seed(bob)
        account = {
            "enableMultipleWriteLocations": False, "writableLocations": [{}],
            "consistencyPolicy": {"defaultConsistencyLevel": "Session"},
        }

        async def read_account():
            return copy.deepcopy(account)

        store = CosmosReservationStore(container, read_account=read_account)
    return {
        "clock": clock, "store": store, "service": ReservationService(store),
        "alice": alice, "bob": bob, "container": container, "account": account,
    }


def key(contract, owner="alice", label=None):
    return operation_id(
        contract[owner].epoch, contract["clock"][0], label or uuid.uuid4().hex,
    )


async def reserve(contract, *, owner="alice", op=None, payload=None, limits=None, tokens=10):
    return await contract["service"].reserve(
        owner, key=op or key(contract, owner), payload=payload or {"prompt": "private"},
        surface="chat", bounds=Bounds(amounts=Amounts(tokens=tokens), basis="request-v1"),
        limits=limits or EntitlementLimits(requestsPerMinute=7, tokensPerDay=70),
    )


def accounting_bounds(amounts):
    if amounts.microUsd is None:
        return Bounds(amounts=amounts, basis="request-v1")
    return Bounds(
        amounts=amounts, basis="catalog-text-v1", priceVersion="fixture-v1",
        inputRate="1", outputRate="2", attemptVersion="fixture-single-send-v1", maxAttempts=1,
    )


def inject_accounting(contract, snapshot, state):
    # Bypass writes only in the test doubles to exercise malformed persisted reads.
    if contract["container"] is None:
        contract["store"]._rows["alice"] = (state, int(snapshot.etag))
    else:
        contract["container"].rows[("alice", STATE_ID)] = {
            **state.model_dump(mode="json"), "_etag": snapshot.etag,
        }


async def assert_invalid_accounting(contract, snapshot, state):
    with pytest.raises(ValidationError):
        QuotaState.model_validate(
            state.model_dump(mode="json"), context={"persisted_quota": True},
        )
    with pytest.raises(ValidationError):
        state_document(state)
    if contract["container"] is not None:
        with pytest.raises(QuotaError, match="incompatible") as caught:
            await contract["store"].read("alice")
        assert caught.value.code == 503
        assert contract["container"].rows[("alice", STATE_ID)] == {
            **state.model_dump(mode="json"), "_etag": snapshot.etag,
        }
    else:
        retained = await contract["store"].read("alice")
        assert retained.state == state and retained.etag == snapshot.etag


METER_WINDOWS = [
    pytest.param("tokens", "tokensPerDay", DAY_SECONDS, id="daily-tokens"),
    pytest.param("tokens", "tokensPerMonth", MONTH_SECONDS, id="monthly-tokens"),
    pytest.param("microUsd", "costPerDayMicroUsd", DAY_SECONDS, id="daily-dollars"),
    pytest.param("microUsd", "costPerMonthMicroUsd", MONTH_SECONDS, id="monthly-dollars"),
]


@pytest.mark.parametrize("cap", ["requestsPerMinute", "tokensPerDay", "tokensPerMonth"])
async def test_concurrent_admission_is_atomic_with_below_limit_control(contract, cap):
    unit = 1 if cap == "requestsPerMinute" else 10
    async def attempt():
        try:
            return await reserve(contract, limits=EntitlementLimits(**{cap: 7 * unit}))
        except QuotaError as exc:
            assert exc.code == 429
            return None

    # More contenders than capacity, but below the CAS retry bound: removing
    # the budget guard must fail on over-admission, not a busy-store assertion.
    results = await asyncio.gather(*(attempt() for _ in range(12)))
    accepted = [record for record in results if record is not None]
    assert len(accepted) == 7
    state = (await contract["store"].read("alice")).state
    assert sum(record.charged.requests for record in state.entries.values()) == 7
    assert sum(record.charged.tokens for record in state.entries.values()) == 70
    # Identical fixture/call, only the configured limit increases.
    assert await reserve(
        contract, limits=EntitlementLimits(**{cap: 8 * unit}),
    )
    assert not (await contract["store"].read("bob")).state.entries


async def test_idempotent_reservation_dispatch_and_settlement(contract):
    op = key(contract)
    records = await asyncio.gather(*(reserve(contract, op=op) for _ in range(12)))
    assert len({r.operationId for r in records}) == 1
    assert len((await contract["store"].read("alice")).state.entries) == 1

    async def dispatch(record):
        try:
            await contract["service"].dispatch("alice", record)
            return True
        except QuotaError as exc:
            assert exc.code == 409
            return False

    assert sum(await asyncio.gather(*(dispatch(r) for r in records))) == 1
    results = await asyncio.gather(*(
        contract["service"].settle(
            "alice", records[0], outcome="complete", actual=Amounts(tokens=3),
        ) for _ in range(12)
    ))
    assert all(r.charged.tokens == 3 for r in results)
    state = (await contract["store"].read("alice")).state
    assert sum(r.charged.tokens for r in state.entries.values()) == 3
    with pytest.raises(QuotaError, match="settlement changed"):
        await contract["service"].settle(
            "alice", records[0], outcome="complete", actual=Amounts(tokens=2),
        )
    # The settled call contributes 3, not its old 10 as well as its new 3.
    assert await reserve(contract, tokens=7, limits=EntitlementLimits(tokensPerDay=10))


async def test_identity_binds_canonical_payload_and_owner(contract):
    op = key(contract)
    record = await reserve(contract, op=op, payload={"a": 1, "b": 2})
    assert await reserve(contract, op=op, payload={"b": 2, "a": 1}) == record
    with pytest.raises(QuotaError, match="payload changed"):
        await reserve(contract, op=op, payload={"a": 2, "b": 2})
    with pytest.raises(QuotaError, match="owner"):
        await contract["service"].dispatch("bob", record)
    with pytest.raises(QuotaError, match="identity"):
        await reserve(contract, owner="bob", op=op)
    assert await reserve(contract, owner="bob")


async def test_retry_of_reserved_work_rechecks_current_limit_without_double_counting(contract):
    op = key(contract)
    record = await reserve(contract, op=op, limits=EntitlementLimits(tokensPerDay=10))
    with pytest.raises(QuotaError, match="would be exceeded"):
        await reserve(contract, op=op, limits=EntitlementLimits(tokensPerDay=9))
    assert await reserve(contract, op=op, limits=EntitlementLimits(tokensPerDay=10)) == record
    assert len((await contract["store"].read("alice")).state.entries) == 1


@pytest.mark.parametrize("outcome", ["cancelled", "timeout", "error", "unknown", "complete"])
async def test_ambiguous_or_missing_usage_keeps_reservation_forever(contract, outcome):
    record = await reserve(contract, limits=EntitlementLimits(tokensPerDay=10))
    await contract["service"].dispatch("alice", record)
    settled = await contract["service"].settle("alice", record, outcome=outcome)
    assert settled.phase == "unknown"
    assert settled.charged.tokens == 10
    contract["clock"][0] += MONTH_SECONDS * 2
    await contract["service"].reconcile("alice")
    with pytest.raises(QuotaError, match="would be exceeded"):
        await reserve(contract, limits=EntitlementLimits(tokensPerDay=10), tokens=1)
    with pytest.raises(QuotaError, match="cannot be released"):
        await contract["service"].release("alice", record)
    assert await reserve(contract, limits=EntitlementLimits(tokensPerDay=11), tokens=1)


async def test_partial_dimension_cannot_refund_known_bound(contract):
    record = await reserve(contract)
    await contract["service"].dispatch("alice", record)
    settled = await contract["service"].settle(
        "alice", record, outcome="complete", actual=Amounts(tokens=None),
    )
    assert settled.phase == "unknown"
    assert settled.charged.tokens == 10


async def test_abandoned_reserved_work_releases_but_stale_ticket_cannot_dispatch(contract):
    record = await reserve(contract, limits=EntitlementLimits(requestsPerMinute=1))
    with pytest.raises(QuotaError):
        await reserve(contract, limits=EntitlementLimits(requestsPerMinute=1))
    contract["clock"][0] = record.expiresAt + 1
    await contract["service"].reconcile("alice")
    with pytest.raises(QuotaError, match="claimed or expired"):
        await contract["service"].dispatch("alice", record)
    assert await reserve(contract, limits=EntitlementLimits(requestsPerMinute=1))


async def test_dispatched_lease_never_expires_into_free_capacity(contract):
    record = await reserve(contract, limits=EntitlementLimits(requestsPerMinute=1))
    await contract["service"].dispatch("alice", record)
    contract["clock"][0] += MONTH_SECONDS * 2
    await contract["service"].reconcile("alice")
    with pytest.raises(QuotaError, match="would be exceeded"):
        await reserve(contract, limits=EntitlementLimits(requestsPerMinute=1))
    assert await reserve(contract, limits=EntitlementLimits(requestsPerMinute=2))


async def test_rolling_window_and_replay_retention_both_protect_terminal_record(contract):
    op = key(contract)
    record = await reserve(contract, op=op)
    await contract["service"].dispatch("alice", record)
    contract["clock"][0] += DAY_SECONDS
    await contract["service"].settle(
        "alice", record, outcome="complete", actual=Amounts(tokens=10),
    )
    contract["clock"][0] = NOW + MONTH_SECONDS + 1
    state = await contract["service"].reconcile("alice")
    assert op in state.entries  # still inside the meter window since completion
    contract["clock"][0] += DAY_SECONDS
    state = await contract["service"].reconcile("alice")
    assert op not in state.entries
    with pytest.raises(QuotaError, match="expired"):
        await reserve(contract, op=op)  # pruning never turns replay into new work
    assert await reserve(contract)


@pytest.mark.parametrize("axis", ["tokens", "microUsd"])
@pytest.mark.parametrize("bound,actual", [(0, 0), (0, 1), (10, 3), (10, 10), (10, 11)])
async def test_bound_violation_is_accounted_and_blocks_future_admissions(
    contract, axis, bound, actual,
):
    service, store = contract["service"], contract["store"]
    candidate = dict(
        payload={}, surface="chat", bounds=accounting_bounds(Amounts(**{axis: bound})),
        limits=EntitlementLimits(**{
            "tokensPerDay" if axis == "tokens" else "costPerDayMicroUsd": 1000,
        }),
    )
    record = await service.reserve("alice", key=key(contract), **candidate)
    await service.dispatch("alice", record)
    settled = await service.settle(
        "alice", record, outcome="complete", actual=Amounts(**{axis: actual}),
    )
    assert settled.phase == "settled" and getattr(settled.charged, axis) == actual
    state = (await store.read("alice")).state
    assert state.blocked is (actual > bound)
    assert QuotaState.model_validate(
        state_document(state), context={"persisted_quota": True},
    ) == state
    if actual > bound:
        with pytest.raises(QuotaError, match="requires reviewed reconciliation"):
            await service.reserve("alice", key=key(contract), **candidate)
    else:
        assert (await service.reserve("alice", key=key(contract), **candidate)).phase == "reserved"
    assert (await service.reserve("bob", key=key(contract, "bob"), **candidate)).phase == "reserved"


@pytest.mark.parametrize("axis", ["tokens", "microUsd"])
@pytest.mark.parametrize("bound", [0, 10])
async def test_known_bound_violation_cannot_serialize_an_unblocked_copy(contract, axis, bound):
    service, store = contract["service"], contract["store"]
    record = await service.reserve(
        "alice", key=key(contract), payload={}, surface="chat",
        bounds=accounting_bounds(Amounts(**{axis: bound})), limits=EntitlementLimits(),
    )
    await service.dispatch("alice", record)
    await service.settle("alice", record, outcome="complete", actual=Amounts(**{axis: bound + 1}))
    snapshot = await store.read("alice")
    assert snapshot.state.blocked is True
    assert QuotaState.model_validate(
        state_document(snapshot.state), context={"persisted_quota": True},
    ) == snapshot.state

    malformed = snapshot.state.model_copy(update={"blocked": False})
    document = malformed.model_dump(mode="json")
    assert document == {**state_document(snapshot.state), "blocked": False}
    with pytest.raises(ValidationError, match="bound violation"):
        QuotaState(**document)
    inject_accounting(contract, snapshot, malformed)
    await assert_invalid_accounting(contract, snapshot, malformed)


@pytest.mark.parametrize("axis", ["tokens", "microUsd"])
@pytest.mark.parametrize("exceeded", [False, True], ids=["at-bound-control", "over-bound"])
@pytest.mark.parametrize("transition,age", [
    pytest.param("reserve", 0, id="reserve"),
    pytest.param("dispatch", 0, id="dispatch"),
    pytest.param("reconcile", 0, id="reconcile"),
    pytest.param("reserve", MONTH_SECONDS * 2, id="reserve-after-retention"),
    pytest.param("reconcile", MONTH_SECONDS * 2, id="reconcile-after-retention"),
])
async def test_unblocked_bound_violation_refuses_before_reconciliation(
    contract, monkeypatch, axis, exceeded, transition, age,
):
    service, store = contract["service"], contract["store"]
    candidate = dict(
        payload={}, surface="chat", bounds=accounting_bounds(Amounts(**{axis: 10})),
        limits=EntitlementLimits(**{
            "tokensPerDay" if axis == "tokens" else "costPerDayMicroUsd": 1000,
        }),
    )
    record = await service.reserve("alice", key=key(contract), **candidate)
    pending = await service.reserve("alice", key=key(contract), **candidate)
    await service.dispatch("alice", record)
    await service.settle(
        "alice", record, outcome="complete", actual=Amounts(**{axis: 11 if exceeded else 10}),
    )
    snapshot = await store.read("alice")
    assert snapshot.state.blocked is exceeded
    state = snapshot.state.model_copy(update={"blocked": False})
    inject_accounting(contract, snapshot, state)
    contract["clock"][0] += age
    reconcile = Mock(wraps=service._reconcile)
    replace = AsyncMock(wraps=store.replace)
    monkeypatch.setattr(service, "_reconcile", reconcile)
    monkeypatch.setattr(store, "replace", replace)

    async def change():
        if transition == "reserve":
            return await service.reserve("alice", key=key(contract), **candidate)
        if transition == "dispatch":
            return await service.dispatch("alice", pending)
        return await service.reconcile("alice")

    if exceeded:
        error = QuotaError if contract["container"] is not None else ValidationError
        with pytest.raises(error) as caught:
            await change()
        if isinstance(caught.value, QuotaError):
            assert caught.value.code == 503
        reconcile.assert_not_called()
        replace.assert_not_awaited()
        await assert_invalid_accounting(contract, snapshot, state)
    else:
        result = await change()
        reconcile.assert_called_once()
        replace.assert_awaited_once()
        retained = (await store.read("alice")).state
        assert retained.blocked is False
        if transition != "reconcile":
            assert result.phase == ("reserved" if transition == "reserve" else "dispatched")
            assert retained.entries[result.operationId] == result
        if age:
            assert record.operationId not in retained.entries
        else:
            assert retained.entries[record.operationId].charged == Amounts(**{axis: 10})
        assert QuotaState.model_validate(
            state_document(retained), context={"persisted_quota": True},
        ) == retained


@pytest.mark.parametrize("axis", ["tokens", "microUsd"])
@pytest.mark.parametrize("actual", [None, 0, 10, 11])
async def test_bound_violation_block_survives_late_accounting_and_pruning(contract, axis, actual):
    service, store = contract["service"], contract["store"]
    candidate = dict(
        payload={}, surface="chat", bounds=accounting_bounds(Amounts(**{axis: 10})),
        limits=EntitlementLimits(),
    )
    first = await service.reserve("alice", key=key(contract), **candidate)
    late = await service.reserve("alice", key=key(contract), **candidate)
    pending = await service.reserve("alice", key=key(contract), **candidate)
    await service.dispatch("alice", first)
    await service.dispatch("alice", late)
    await service.settle("alice", first, outcome="complete", actual=Amounts(**{axis: 11}))
    with pytest.raises(QuotaError, match="requires reviewed reconciliation"):
        await service.dispatch("alice", pending)

    contract["clock"][0] += MONTH_SECONDS * 2
    usage = None if actual is None else Amounts(**{axis: actual})
    settled = await service.settle("alice", late, outcome="complete", actual=usage)
    assert settled.phase == ("unknown" if actual is None else "settled")
    assert getattr(settled.charged, axis) == (10 if actual is None else actual)
    assert await service.settle("alice", late, outcome="complete", actual=usage) == settled
    state = (await store.read("alice")).state
    assert first.operationId not in state.entries and state.blocked is True
    assert state.entries[pending.operationId].phase == "released"

    contract["clock"][0] += MONTH_SECONDS * 2
    retained = await service.reconcile("alice")
    assert retained.entries == ({late.operationId: settled} if actual is None else {})
    assert retained.blocked is True
    assert QuotaState.model_validate(
        state_document(retained), context={"persisted_quota": True},
    ) == retained
    with pytest.raises(QuotaError, match="requires reviewed reconciliation"):
        await service.reserve("alice", key=key(contract), **candidate)
    other = await service.reserve("bob", key=key(contract, "bob"), **candidate)
    assert (await service.dispatch("bob", other)).phase == "dispatched"
    assert (await store.read("bob")).state.blocked is False


async def test_unknown_meter_is_refused_only_under_corresponding_cap(contract):
    with pytest.raises(QuotaError, match="does not support"):
        await reserve(contract, tokens=None, limits=EntitlementLimits(tokensPerDay=100))
    assert await reserve(
        contract, tokens=None, limits=EntitlementLimits(requestsPerMinute=1),
    )
    with pytest.raises(QuotaError, match="unknown prior"):
        await reserve(contract, limits=EntitlementLimits(tokensPerDay=100))


async def test_missing_state_is_not_an_empty_balance(contract):
    with pytest.raises(QuotaError, match="(absent|unavailable)"):
        await contract["service"].reserve(
            "missing", key=key(contract), payload={}, surface="chat",
            bounds=Bounds(amounts=Amounts(), basis="request-v1"), limits=EntitlementLimits(),
        )
    assert await reserve(contract)


async def test_credential_free_bounded_state(contract):
    record = await reserve(contract, payload={"Authorization": "sensitive", "prompt": "private"})
    state = (await contract["store"].read("alice")).state
    body = json.dumps(state_document(state))
    assert "sensitive" not in body and "private" not in body and "Authorization" not in body
    assert len(body) < MAX_STATE_BYTES
    oversized = state.model_copy(update={"entries": {
        (op := key(contract, label=str(index))): record.model_copy(update={"operationId": op})
        for index in range(MAX_ENTRIES)
    }})
    with pytest.raises(QuotaError, match="capacity"):
        state_document(oversized)
    assert await reserve(contract)


async def test_cosmos_layout_and_etag_fail_closed_with_same_store_control(contract):
    container = contract["container"]
    if container is None:
        return
    account = contract["account"]
    for mutate, restore in (
        (lambda: account.update(enableMultipleWriteLocations=True),
         lambda: account.update(enableMultipleWriteLocations=False)),
        (lambda: account["writableLocations"].append({}),
         lambda: account["writableLocations"].pop()),
        (lambda: account["consistencyPolicy"].update(defaultConsistencyLevel="Eventual"),
         lambda: account["consistencyPolicy"].update(defaultConsistencyLevel="Session")),
        (lambda: container.layout.update(defaultTtl=60),
         lambda: container.layout.pop("defaultTtl")),
        (lambda: container.layout.update(analyticalStorageTtl=-1),
         lambda: container.layout.pop("analyticalStorageTtl")),
        # A Boolean equals 0 in Python; it is not the disabled analytical sentinel.
        (lambda: container.layout.update(analyticalStorageTtl=False),
         lambda: container.layout.pop("analyticalStorageTtl")),
        (lambda: container.layout["partitionKey"].update(paths=["/sessionId"]),
         lambda: container.layout["partitionKey"].update(paths=["/userId"])),
    ):
        mutate()
        # Unlimited policy: a refusal here can only come from the layout guard.
        with pytest.raises(QuotaError, match="Session-consistency|partition or retention"):
            await reserve(contract, limits=EntitlementLimits())
        restore()
        assert await reserve(contract, limits=EntitlementLimits())
    # The explicit non-expiring and disabled-analytical sentinels are compatible.
    container.layout.update(defaultTtl=-1, analyticalStorageTtl=0)
    assert await reserve(contract, limits=EntitlementLimits())
    raw = container.rows[("alice", STATE_ID)]
    etag = raw.pop("_etag")
    with pytest.raises(QuotaError, match="ETag"):
        await reserve(contract, limits=EntitlementLimits())
    raw["_etag"] = etag
    assert await reserve(contract, limits=EntitlementLimits())


async def test_lost_dispatch_ack_cannot_allow_a_retry_to_dispatch_twice(contract):
    container = contract["container"]
    if container is None:
        return
    record = await reserve(contract)
    container.lose_ack_phase = "dispatched"
    with pytest.raises(QuotaError, match="write is unavailable"):
        await contract["service"].dispatch("alice", record)
    assert container.rows[("alice", STATE_ID)]["entries"][record.operationId]["phase"] == "dispatched"
    container.lose_ack_phase = None
    with pytest.raises(QuotaError, match="already claimed"):
        await contract["service"].dispatch("alice", record)
    other = await reserve(contract)
    assert await contract["service"].dispatch("alice", other)


def test_cannot_reseed_existing_owner_even_when_empty():
    store = LocalReservationStore()
    store.seed("alice")
    with pytest.raises(QuotaError, match="already exists"):
        store.seed("alice")
    assert store.seed("bob")


def test_invalid_quantity_and_state_version_are_rejected():
    for value in (-1, True, 1.1, "1"):
        with pytest.raises(ValidationError):
            Amounts(tokens=value)
    state = LocalReservationStore().seed("alice").model_dump(mode="json")
    state["policyVersion"] = "future"
    with pytest.raises(ValidationError):
        QuotaState.model_validate(state)


@pytest.mark.parametrize("contract", ["cosmos"], indirect=True)
@pytest.mark.parametrize("missing", [
    "entries", "blocked", "phase_and_dispatch", "outcome", "settlementDigest",
    "bounds.compute", "charged.compute", "bounds.requests", "charged.requests",
    "bounds.tokens", "charged.microUsd",
])
async def test_incomplete_persisted_accounting_never_uses_construction_defaults(contract, missing):
    service, container = contract["service"], contract["container"]
    bounds = Bounds(amounts=Amounts(compute=1), basis="compute-v1")
    record = await service.reserve(
        "alice", key=key(contract), payload={}, surface="compute",
        bounds=bounds, limits=EntitlementLimits(computeExecutionsPerDay=1),
    )
    await service.dispatch("alice", record)
    raw = container.rows[("alice", STATE_ID)]
    original = copy.deepcopy(raw)
    entry = raw["entries"][record.operationId]
    if missing in {"entries", "blocked"}:
        del raw[missing]
    elif missing == "phase_and_dispatch":
        del entry["phase"]
        del entry["dispatchedAt"]
    elif "." in missing:
        part, field = missing.split(".")
        amounts = entry["bounds"]["amounts"] if part == "bounds" else entry["charged"]
        del amounts[field]
    else:
        del entry[missing]
    with pytest.raises(QuotaError, match="incompatible"):
        await contract["store"].read("alice")
    container.rows[("alice", STATE_ID)] = original
    restored = (await contract["store"].read("alice")).state
    assert restored.entries[record.operationId].charged.compute == 1
    assert restored.entries[record.operationId].charged.tokens is None
    with pytest.raises(QuotaError, match="would be exceeded"):
        await service.reserve(
            "alice", key=key(contract), payload={}, surface="compute",
            bounds=bounds, limits=EntitlementLimits(computeExecutionsPerDay=1),
        )
    control = await service.reserve(
        "alice", key=key(contract), payload={}, surface="compute",
        bounds=bounds, limits=EntitlementLimits(computeExecutionsPerDay=2),
    )
    assert await service.dispatch("alice", control)


@pytest.mark.parametrize("contract", ["cosmos"], indirect=True)
@pytest.mark.parametrize("axis", ["requests", "compute"])
async def test_persisted_attempt_counts_must_match_the_actual_surface(contract, axis):
    service, container = contract["service"], contract["container"]
    record = await service.reserve(
        "alice", key=key(contract), payload={}, surface="compute",
        bounds=Bounds(amounts=Amounts(compute=1), basis="compute-v1"),
        limits=EntitlementLimits(),
    )
    await service.dispatch("alice", record)
    original = copy.deepcopy(container.rows[("alice", STATE_ID)])
    entry = container.rows[("alice", STATE_ID)]["entries"][record.operationId]
    entry["bounds"]["amounts"][axis] = 0
    entry["charged"][axis] = 0
    with pytest.raises(QuotaError, match="incompatible"):
        await contract["store"].read("alice")
    container.rows[("alice", STATE_ID)] = original
    assert (await contract["store"].read("alice")).state.entries[record.operationId].charged.compute == 1


@pytest.mark.parametrize("axis,cap,seconds", METER_WINDOWS)
@pytest.mark.parametrize("missing", ["charged", "outcome", "settlementDigest"])
async def test_incomplete_settlement_cannot_age_into_admission(contract, axis, cap, seconds, missing):
    service, store = contract["service"], contract["store"]
    bounds = accounting_bounds(Amounts(**{axis: 10}))
    limits = EntitlementLimits(**{cap: 10})
    record = await service.reserve(
        "alice", key=key(contract), payload={}, surface="chat", bounds=bounds, limits=limits,
    )
    await service.dispatch("alice", record)
    settled = await service.settle(
        "alice", record, outcome="complete", actual=Amounts(**{axis: 3}),
    )
    snapshot = await store.read("alice")
    broken = settled.model_copy(update={
        missing: settled.charged.model_copy(update={axis: None}) if missing == "charged" else None,
    })
    malformed = snapshot.state.model_copy(update={"entries": {record.operationId: broken}})
    contract["clock"][0] += seconds + 1
    candidate = dict(key=key(contract), payload={}, surface="chat", bounds=bounds, limits=limits)
    inject_accounting(contract, snapshot, malformed)
    try:
        error = QuotaError if contract["container"] is not None else ValidationError
        with pytest.raises(error) as caught:
            await service.reserve("alice", **candidate)
        if isinstance(caught.value, QuotaError):
            assert caught.value.code == 503
        await assert_invalid_accounting(contract, snapshot, malformed)
    finally:
        inject_accounting(contract, snapshot, snapshot.state)
        # The identical admission must work when only the valid accounting is restored.
        admitted = await service.reserve("alice", **candidate)
        assert admitted.phase == "reserved" and admitted.charged == bounds.amounts


@pytest.mark.parametrize("phase,field,value", [
    ("reserved", "dispatchedAt", NOW),
    ("reserved", "settledAt", NOW + 2),
    ("reserved", "outcome", "complete"),
    ("reserved", "settlementDigest", "a" * 64),
    ("dispatched", "dispatchedAt", None),
    ("dispatched", "dispatchedAt", NOW - 1),
    ("dispatched", "dispatchedAt", NOW + 3),
    ("dispatched", "settledAt", NOW + 2),
    ("dispatched", "outcome", "complete"),
    ("dispatched", "settlementDigest", "a" * 64),
    ("settled", "outcome", "cancelled"),
    ("settled", "outcome", "timeout"),
    ("settled", "outcome", "error"),
    ("settled", "outcome", "unknown"),
    ("settled", "dispatchedAt", None),
    ("settled", "dispatchedAt", NOW - 1),
    ("settled", "dispatchedAt", NOW + 121),
    ("settled", "settledAt", None),
    ("settled", "settledAt", NOW),
    ("unknown", "outcome", None),
    ("unknown", "settlementDigest", None),
    ("unknown", "charged.tokens", None),
    ("unknown", "charged.microUsd", None),
    ("unknown", "charged.tokens", 3),
    ("unknown", "charged.microUsd", 3),
    ("unknown", "phase", "dispatched"),
    ("released", "dispatchedAt", NOW),
    ("released", "outcome", "complete"),
    ("released", "settlementDigest", "a" * 64),
    ("expiry", "dispatchedAt", NOW),
    ("expiry", "outcome", "complete"),
    ("expiry", "settlementDigest", "a" * 64),
])
async def test_phase_evidence_must_match_service_transitions(contract, phase, field, value):
    service, store = contract["service"], contract["store"]
    bounds = accounting_bounds(Amounts(tokens=10, microUsd=10))
    record = await service.reserve(
        "alice", key=key(contract), payload={}, surface="chat",
        bounds=bounds, limits=EntitlementLimits(),
    )
    if phase in {"dispatched", "settled", "unknown"}:
        contract["clock"][0] = NOW + 1
        await service.dispatch("alice", record)
    contract["clock"][0] = NOW + 2
    if phase in {"settled", "unknown"}:
        # Completion after lease expiry is legitimate for already-dispatched work.
        contract["clock"][0] = record.expiresAt + 2
        await service.settle(
            "alice", record, outcome="complete",
            actual=Amounts(tokens=3, microUsd=3) if phase == "settled" else None,
        )
    elif phase == "released":
        await service.release("alice", record)
    elif phase == "expiry":
        contract["clock"][0] = record.expiresAt + 1
    await service.reconcile("alice")
    snapshot = await store.read("alice")
    original = snapshot.state.entries[record.operationId]
    assert original.phase == ("released" if phase == "expiry" else phase)
    if field.startswith("charged."):
        update = {"charged": original.charged.model_copy(update={field.split(".")[1]: value})}
    else:
        update = {field: value}
    malformed = snapshot.state.model_copy(update={"entries": {
        record.operationId: original.model_copy(update=update),
    }})
    inject_accounting(contract, snapshot, malformed)
    try:
        error = QuotaError if contract["container"] is not None else ValidationError
        with pytest.raises(error) as caught:
            await service.reconcile("alice")
        if isinstance(caught.value, QuotaError):
            assert caught.value.code == 503
        await assert_invalid_accounting(contract, snapshot, malformed)
    finally:
        inject_accounting(contract, snapshot, snapshot.state)
        assert (await store.read("alice")).state == snapshot.state
        assert await service.reconcile("alice") == snapshot.state


@pytest.mark.parametrize("axis,cap,seconds", METER_WINDOWS)
@pytest.mark.parametrize("actual", [None, 0, 3])
@pytest.mark.parametrize("age", ["window", "retention"])
async def test_known_usage_ages_but_true_unknown_retains_each_meter(
    contract, axis, cap, seconds, actual, age,
):
    service, store = contract["service"], contract["store"]
    bounds = accounting_bounds(Amounts(**{axis: 10}))
    record = await service.reserve(
        "alice", key=key(contract), payload={}, surface="chat",
        bounds=bounds, limits=EntitlementLimits(**{cap: 10}),
    )
    await service.dispatch("alice", record)
    settled = await service.settle(
        "alice", record, outcome="complete",
        actual=Amounts(**{axis: actual}) if actual is not None else None,
    )
    assert settled.phase == ("unknown" if actual is None else "settled")
    assert getattr(settled.charged, axis) == (10 if actual is None else actual)
    contract["clock"][0] += seconds + 1 if age == "window" else MONTH_SECONDS * 2
    state = await service.reconcile("alice")
    if actual is None:
        assert state.entries[record.operationId] == settled
    candidate = dict(key=key(contract), payload={}, surface="chat", bounds=bounds)
    if actual is None:
        with pytest.raises(QuotaError, match="would be exceeded") as caught:
            await service.reserve("alice", **candidate, limits=EntitlementLimits(**{cap: 10}))
        assert caught.value.code == 429
    admitted = await service.reserve(
        "alice", **candidate, limits=EntitlementLimits(**{cap: 20 if actual is None else 10}),
    )
    assert admitted.phase == "reserved"
    state = (await store.read("alice")).state
    assert QuotaState.model_validate(
        state_document(state), context={"persisted_quota": True},
    ) == state


@pytest.mark.parametrize("bounded,actual,phase", [
    pytest.param(Amounts(tokens=10), Amounts(tokens=3), "settled", id="tokens-only"),
    pytest.param(Amounts(microUsd=10), Amounts(microUsd=3), "settled", id="dollars-only"),
    pytest.param(Amounts(), Amounts(), "settled", id="request-only"),
    pytest.param(Amounts(), Amounts(tokens=3, microUsd=3), "settled", id="unbounded-measurements"),
    pytest.param(
        Amounts(tokens=10), Amounts(tokens=3, microUsd=11), "settled",
        id="unbounded-dollar-measurement",
    ),
    pytest.param(
        Amounts(microUsd=10), Amounts(tokens=11, microUsd=3), "settled",
        id="unbounded-token-measurement",
    ),
    pytest.param(
        Amounts(tokens=0, microUsd=0), Amounts.zero(), "settled", id="measured-zero",
    ),
    pytest.param(
        Amounts(tokens=10, microUsd=10), Amounts(requests=0, compute=7, tokens=3, microUsd=3),
        "settled", id="normalized-attempt-counts",
    ),
    pytest.param(Amounts(), None, "unknown", id="missing-request-only-usage"),
    pytest.param(Amounts(tokens=10), Amounts(), "unknown", id="missing-bounded-tokens"),
    pytest.param(Amounts(microUsd=10), Amounts(), "unknown", id="missing-bounded-dollars"),
    pytest.param(
        Amounts(tokens=10, microUsd=10), Amounts(tokens=3), "unknown", id="partial-tokens",
    ),
    pytest.param(
        Amounts(tokens=10, microUsd=10), Amounts(microUsd=3), "unknown", id="partial-dollars",
    ),
    pytest.param(
        Amounts(tokens=10, microUsd=10), Amounts(tokens=11), "unknown", id="partial-over-tokens",
    ),
    pytest.param(
        Amounts(tokens=10, microUsd=10), Amounts(microUsd=11), "unknown", id="partial-over-dollars",
    ),
])
@pytest.mark.parametrize("surface", ["chat", "compute"])
@pytest.mark.parametrize("delay", [1, MONTH_SECONDS * 2], ids=["after-lease", "after-replay-horizon"])
async def test_settlement_completeness_preserves_valid_axes_and_attempt_counts(
    contract, bounded, actual, phase, surface, delay,
):
    from ai4ia_api.hard_quota.models import canonical_digest

    service, store = contract["service"], contract["store"]
    bounds = accounting_bounds(bounded.model_copy(update={"compute": int(surface == "compute")}))
    record = await service.reserve(
        "alice", key=key(contract), payload={}, surface=surface,
        bounds=bounds, limits=EntitlementLimits(),
    )
    contract["clock"][0] = record.expiresAt
    await service.dispatch("alice", record)  # The inclusive lease boundary is still valid.
    contract["clock"][0] += delay
    settled = await service.settle("alice", record, outcome="complete", actual=actual)
    assert settled.phase == phase and settled.outcome == "complete"
    assert settled.charged == (actual if phase == "settled" else bounds.amounts).model_copy(update={
        "requests": 1, "compute": bounds.amounts.compute,
    })
    # Identity hashes original usage, not the normalized attempt counters.
    assert settled.settlementDigest == canonical_digest({
        "outcome": "complete", "actual": actual.model_dump(mode="json") if actual is not None else None,
    })
    if phase == "unknown":
        contract["clock"][0] += MONTH_SECONDS * 2
    assert await service.settle("alice", record, outcome="complete", actual=actual) == settled
    state = (await store.read("alice")).state
    assert state.blocked is False
    assert QuotaState.model_validate(
        state_document(state), context={"persisted_quota": True},
    ) == state


@pytest.mark.parametrize("finish", ["complete", "unknown", "release", "expiry"])
async def test_capacity_reserves_growth_for_every_outstanding_operation(contract, monkeypatch, finish):
    from ai4ia_api.hard_quota import models

    # A smaller test budget exercises many simultaneous transitions cheaply.
    # The exact 512KiB / 600-operation regression is covered separately.
    monkeypatch.setattr(models, "MAX_STATE_BYTES", 16 * 1024)
    service = contract["service"]
    admitted = []
    for index in range(100):
        try:
            record = await reserve(contract, tokens=None, limits=EntitlementLimits())
        except QuotaError as exc:
            assert "capacity" in str(exc)
            break
        if index % 2:
            record = await service.dispatch("alice", record)
        admitted.append(record)
    else:
        raise AssertionError("capacity fixture did not fill its bounded document")
    assert len(admitted) > 2
    if finish == "expiry":
        contract["clock"][0] += 121
        state = await service.reconcile("alice")
        assert {r.phase for r in state.entries.values()} == {"released", "dispatched"}
    else:
        for record in admitted:
            if finish == "release" and record.phase == "reserved":
                await service.release("alice", record)
                continue
            if record.phase == "reserved":
                record = await service.dispatch("alice", record)
            await service.settle(
                "alice", record,
                outcome="complete" if finish == "complete" else "unknown" if finish == "unknown" else "cancelled",
                actual=Amounts(tokens=2**53 - 1, microUsd=2**53 - 1)
                if finish == "complete" else None,
            )
    state = (await contract["store"].read("alice")).state
    assert len(state.entries) == len(admitted)
    assert len(json.dumps(state_document(state), ensure_ascii=True).encode("utf-8")) <= 16 * 1024


async def test_600_operation_size_reproduction_refuses_before_egress(contract):
    from ai4ia_api.hard_quota.models import RESERVATION_SECONDS, Reservation, canonical_digest

    service, store = contract["service"], contract["store"]
    bounds = Bounds(amounts=Amounts(), basis="request-v1")
    cap = EntitlementLimits(requestsPerMinute=1)
    actual = Amounts()
    settlement = canonical_digest({"outcome": "complete", "actual": actual.model_dump(mode="json")})
    initial = (await store.read("alice")).state

    def operation(index):
        timestamp = NOW + 61 * index
        surface = "transcription" if index < 25 else "chat"
        return Reservation(
            operationId=operation_id(initial.epoch, timestamp, str(index)),
            payloadDigest=canonical_digest({
                "owner": "alice", "surface": surface, "payload": {},
                "bounds": bounds.model_dump(mode="json"),
            }),
            surface=surface, bounds=bounds, reservedAt=timestamp,
            expiresAt=timestamp + RESERVATION_SECONDS, charged=actual,
        )

    def completed(record):
        return record.model_copy(update={
            "phase": "settled", "dispatchedAt": record.reservedAt,
            "settledAt": record.reservedAt, "outcome": "complete",
            "settlementDigest": settlement,
        })

    # Exact settled prefix of the reviewer's probe: 25 transcription calls,
    # then chat, one every 61s. Preloading avoids quadratic 600-round-trip test
    # overhead while preserving every real persisted byte and time fence.
    prefix = [completed(operation(index)) for index in range(598)]
    seeded = initial.model_copy(update={
        "entries": {record.operationId: record for record in prefix},
        "observedAt": prefix[-1].settledAt,
    })
    assert await store.replace("alice", await store.read("alice"), seeded)
    contract["clock"][0] = NOW + 61 * 598
    control = await service.reserve(
        "alice", key=operation(598).operationId, payload={}, surface="chat", bounds=bounds, limits=cap,
    )
    control = await service.dispatch("alice", control)
    assert (await service.settle("alice", control, outcome="complete", actual=actual)).phase == "settled"
    before = (await store.read("alice")).state
    assert len(before.entries) == 599

    contract["clock"][0] += 61
    candidate = operation(599)
    old_dispatched = before.model_copy(update={
        "observedAt": candidate.reservedAt,
        "entries": {**before.entries, candidate.operationId: candidate.model_copy(update={
            "phase": "dispatched", "dispatchedAt": candidate.reservedAt,
        })},
    })
    old_settled = old_dispatched.model_copy(update={
        "entries": {**old_dispatched.entries, candidate.operationId: completed(candidate)},
    })
    def wire_size(state):
        return len(json.dumps(state.model_dump(mode="json"), ensure_ascii=True).encode("utf-8"))
    assert wire_size(old_dispatched) == 524225
    assert wire_size(old_settled) == 524296 > MAX_STATE_BYTES

    with pytest.raises(QuotaError, match="capacity"):
        await service.reserve(
            "alice", key=candidate.operationId, payload={}, surface="chat", bounds=bounds, limits=cap,
        )
    assert (await store.read("alice")).state.entries == before.entries
    # No stranded operation600: normal expiry of known history restores capacity.
    contract["clock"][0] += MONTH_SECONDS * 2
    assert not (await service.reconcile("alice")).entries
    later = await service.reserve(
        "alice", key=key(contract), payload={}, surface="chat", bounds=bounds, limits=cap,
    )
    later = await service.dispatch("alice", later)
    assert (await service.settle("alice", later, outcome="complete", actual=actual)).phase == "settled"


async def test_existing_reservation_without_transition_room_cannot_dispatch(contract, monkeypatch):
    from ai4ia_api.hard_quota import models

    record = await reserve(contract)
    before = (await contract["store"].read("alice")).state
    claimed = before.model_copy(update={"entries": {
        **before.entries, record.operationId: record.model_copy(update={
            "phase": "dispatched", "dispatchedAt": contract["clock"][0],
        }),
    }})
    dispatch_bytes = len(json.dumps(state_document(claimed), ensure_ascii=True).encode("utf-8"))
    monkeypatch.setattr(models, "MAX_STATE_BYTES", dispatch_bytes + 1)
    with pytest.raises(QuotaError, match="capacity"):
        await contract["service"].dispatch("alice", record)
    assert (await contract["store"].read("alice")).state.entries[record.operationId].phase == "reserved"
    monkeypatch.setattr(models, "MAX_STATE_BYTES", MAX_STATE_BYTES)
    assert await contract["service"].dispatch("alice", record)
    assert (await contract["service"].settle(
        "alice", record, outcome="complete", actual=Amounts(tokens=3),
    )).phase == "settled"
