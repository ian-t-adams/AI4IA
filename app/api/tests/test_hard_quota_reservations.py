"""One conformance suite across a local fake and real Cosmos adapter/fake ETags."""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest
from azure.core import MatchConditions
from azure.cosmos.exceptions import CosmosAccessConditionFailedError, CosmosResourceNotFoundError

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
        account = {"enableMultipleWriteLocations": False, "writableLocations": [{}]}

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


async def test_bound_violation_is_accounted_and_blocks_future_admissions(contract):
    record = await reserve(contract)
    await contract["service"].dispatch("alice", record)
    settled = await contract["service"].settle(
        "alice", record, outcome="complete", actual=Amounts(tokens=11),
    )
    assert settled.charged.tokens == 11
    with pytest.raises(QuotaError, match="reconciliation"):
        await reserve(contract)
    assert await reserve(contract, owner="bob")


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
    for mutate, restore in (
        (lambda: contract["account"].update(enableMultipleWriteLocations=True),
         lambda: contract["account"].update(enableMultipleWriteLocations=False)),
        (lambda: container.layout.update(defaultTtl=60),
         lambda: container.layout.pop("defaultTtl")),
        (lambda: container.layout["partitionKey"].update(paths=["/sessionId"]),
         lambda: container.layout["partitionKey"].update(paths=["/userId"])),
    ):
        mutate()
        with pytest.raises(QuotaError):
            await reserve(contract)
        restore()
        assert await reserve(contract)
    raw = container.rows[("alice", STATE_ID)]
    etag = raw.pop("_etag")
    with pytest.raises(QuotaError, match="ETag"):
        await reserve(contract)
    raw["_etag"] = etag
    assert await reserve(contract)


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
    from pydantic import ValidationError

    for value in (-1, True, 1.1, "1"):
        with pytest.raises(ValidationError):
            Amounts(tokens=value)
    state = LocalReservationStore().seed("alice").model_dump(mode="json")
    state["policyVersion"] = "future"
    with pytest.raises(ValidationError):
        QuotaState.model_validate(state)
