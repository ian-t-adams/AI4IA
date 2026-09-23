"""The approved request-count rollout scope, on the local fake and Cosmos ETag fake."""
from __future__ import annotations

import copy
import uuid

import pytest
from pydantic import ValidationError

from ai4ia_api.catalog import ModelCatalog
from ai4ia_api.entitlements.memory_store import InMemoryEntitlementStore
from ai4ia_api.entitlements.models import (
    DAY_SECONDS, MINUTE_SECONDS, MONTH_SECONDS, Entitlement, EntitlementLimits,
)
from ai4ia_api.entitlements.service import EntitlementService
from ai4ia_api.hard_quota.cosmos_store import CosmosReservationStore
from ai4ia_api.hard_quota.dispatch import AdmissionController, admission_scope
from ai4ia_api.hard_quota.models import (
    REQUEST_COUNT_REPLAY_SECONDS,
    Amounts,
    Bounds,
    QuotaError,
    QuotaState,
    operation_id,
    parse_operation_id,
    state_document,
)
from ai4ia_api.hard_quota.service import RequestCountScope, ReservationService
from ai4ia_api.usage.pricing import PricingBook
from tests.test_hard_quota_reservations import (
    RacingLocalStore,
    StatefulContainer,
    accounting_bounds,
    assert_invalid_accounting,
    inject_accounting,
)

NOW = 1_800_000_000
PAST = NOW - 2 * DAY_SECONDS
SESSION_ACCOUNT = {
    "enableMultipleWriteLocations": False, "writableLocations": [{}],
    "consistencyPolicy": {"defaultConsistencyLevel": "Session"},
}
REQUEST = Bounds(amounts=Amounts(), basis="request-v1")
COMPUTE = Bounds(amounts=Amounts(compute=1), basis="compute-v1")


class Adapter:
    def __init__(self, kind: str) -> None:
        self.clock = [NOW]
        self.account = copy.deepcopy(SESSION_ACCOUNT)
        if kind == "local":
            self.container = None
            self.store = RacingLocalStore(clock=lambda: self.clock[0])
        else:
            self.container = StatefulContainer(lambda: self.clock[0])

            async def read_account():
                return copy.deepcopy(self.account)

            self.store = CosmosReservationStore(self.container, read_account=read_account)

    def seed(self, owner: str, at: int) -> QuotaState:
        # The bootstrap shape: no entries, every fence at the store-clock creation time.
        state = QuotaState(
            userId=owner, epoch=uuid.uuid4().hex, validAfter=at, replayFloor=at, observedAt=at,
        )
        if self.container is None:
            self.store._rows[owner] = (state, 1)
        else:
            self.container.seed(state)
        return state

    def contract(self) -> dict:
        return {"container": self.container, "store": self.store}


@pytest.fixture(params=["local", "cosmos"])
def adapter(request):
    return Adapter(request.param)


def scoped(adapter: Adapter, coverage: int) -> ReservationService:
    return ReservationService(adapter.store, scope=RequestCountScope(coverage))


def historical(adapter: Adapter) -> ReservationService:
    return ReservationService(adapter.store)


async def admit(
    service, state, clock, *, owner="alice", surface="chat", limits=None, bounds=None, key=None,
):
    return await service.reserve(
        owner, key=key or operation_id(state.epoch, clock[0], uuid.uuid4().hex),
        payload={"prompt": "private"}, surface=surface,
        bounds=bounds or (COMPUTE if surface == "compute" else REQUEST),
        limits=limits or EntitlementLimits(),
    )


@pytest.mark.parametrize("coverage_offset", [-1000, 500], ids=[
    "document-created-after-cutover", "cutover-after-document",
])
async def test_fence_counts_the_uncovered_request_window_as_consumed(adapter, coverage_offset):
    state = adapter.seed("alice", NOW)
    coverage = NOW + coverage_offset
    fence = max(NOW, coverage)
    service = scoped(adapter, coverage)
    capped = EntitlementLimits(requestsPerMinute=5)
    adapter.clock[0] = fence + 30
    with pytest.raises(QuotaError, match="requestsPerMinute history before cutover") as caught:
        await admit(service, state, adapter.clock, limits=capped)
    assert caught.value.code == 429
    # An uncapped dispatch is admitted at the same instant and is still recorded.
    uncapped = await admit(service, state, adapter.clock)
    assert (await adapter.store.read("alice")).state.entries[uncapped.operationId].phase == "reserved"
    # Control: the identical capped call is admitted without the rollout scope.
    assert (await admit(historical(adapter), state, adapter.clock, limits=capped)).phase == "reserved"
    adapter.clock[0] = fence + MINUTE_SECONDS - 1
    with pytest.raises(QuotaError, match="history before cutover"):
        await admit(service, state, adapter.clock, limits=capped)
    adapter.clock[0] = fence + MINUTE_SECONDS
    assert (await admit(service, state, adapter.clock, limits=capped)).phase == "reserved"


async def test_fence_holds_compute_attempts_for_their_full_day(adapter):
    state = adapter.seed("alice", NOW)
    service = scoped(adapter, NOW)
    compute_cap = EntitlementLimits(computeExecutionsPerDay=3)
    adapter.clock[0] = NOW + DAY_SECONDS - 1
    with pytest.raises(QuotaError, match="computeExecutionsPerDay history before cutover"):
        await admit(service, state, adapter.clock, surface="compute", limits=compute_cap)
    # A compute cap never applies to other surfaces, whose minute window is covered.
    assert (await admit(service, state, adapter.clock, limits=compute_cap)).surface == "chat"
    adapter.clock[0] = NOW + DAY_SECONDS
    admitted = await admit(service, state, adapter.clock, surface="compute", limits=compute_cap)
    assert admitted.surface == "compute" and admitted.phase == "reserved"


@pytest.mark.parametrize("cap", [
    "tokensPerDay", "tokensPerMonth", "costPerDayMicroUsd", "costPerMonthMicroUsd",
])
async def test_scope_refuses_token_and_dollar_caps_before_any_history(adapter, cap):
    state = adapter.seed("alice", PAST)
    service = scoped(adapter, PAST)
    limits = EntitlementLimits(**{cap: 1000})
    with pytest.raises(QuotaError, match=f"{cap} is outside the approved request-count rollout"):
        await admit(service, state, adapter.clock, limits=limits)
    # The historical contract refuses the same request-only call only as an
    # unsupported meter; the scope refusal does not depend on the bound shape.
    with pytest.raises(QuotaError, match="does not support"):
        await admit(historical(adapter), state, adapter.clock, limits=limits)
    assert await admit(service, state, adapter.clock, limits=EntitlementLimits(requestsPerMinute=1))


async def test_token_refusal_precedes_the_fence_and_any_history(adapter):
    state = adapter.seed("alice", NOW)
    service = scoped(adapter, NOW)
    adapter.clock[0] = NOW + 30
    with pytest.raises(QuotaError, match="tokensPerDay is outside the approved request-count rollout"):
        await admit(service, state, adapter.clock, limits=EntitlementLimits(
            requestsPerMinute=5, tokensPerDay=10,
        ))
    with pytest.raises(QuotaError, match="requestsPerMinute history before cutover"):
        await admit(service, state, adapter.clock, limits=EntitlementLimits(requestsPerMinute=5))


@pytest.mark.parametrize("amounts", [
    Amounts(tokens=10), Amounts(microUsd=10), Amounts(tokens=10, microUsd=10),
], ids=["tokens", "dollars", "both"])
async def test_scope_refuses_token_or_dollar_bounded_reservations(adapter, amounts):
    state = adapter.seed("alice", PAST)
    bounds = accounting_bounds(amounts)
    with pytest.raises(QuotaError, match="bounds are outside the approved request-count rollout"):
        await admit(scoped(adapter, PAST), state, adapter.clock, bounds=bounds)
    assert not (await adapter.store.read("alice")).state.entries
    assert (await admit(historical(adapter), state, adapter.clock, bounds=bounds)).phase == "reserved"


@pytest.mark.parametrize("surface,cap,window", [
    ("chat", "requestsPerMinute", MINUTE_SECONDS),
    ("compute", "computeExecutionsPerDay", DAY_SECONDS),
])
@pytest.mark.parametrize("outcome", ["cancelled", "timeout", "error", "unknown", "complete"])
async def test_request_only_terminal_outcomes_settle_as_known_attempts(
    adapter, surface, cap, window, outcome,
):
    services = {
        "alice": (adapter.seed("alice", PAST), scoped(adapter, PAST)),
        "bob": (adapter.seed("bob", PAST), historical(adapter)),
    }
    limits = EntitlementLimits(**{cap: 1})
    settled = {}
    for owner, (state, service) in services.items():
        record = await admit(service, state, adapter.clock, owner=owner, surface=surface, limits=limits)
        await service.dispatch(owner, record)
        # No usage object: the lease never completed, as after a cancel or error.
        settled[owner] = await service.settle(owner, record, outcome=outcome)
    alice = settled["alice"]
    assert alice.phase == "settled" and alice.outcome == outcome
    assert alice.charged == alice.bounds.amounts
    assert (alice.charged.requests, alice.charged.compute) == (1, int(surface == "compute"))
    # Control: the historical contract holds the identical outcome as unknown.
    assert settled["bob"].phase == "unknown"
    for owner, (state, service) in services.items():
        # Both attempts consume the window; nothing is refunded early.
        with pytest.raises(QuotaError, match="would be exceeded"):
            await admit(service, state, adapter.clock, owner=owner, surface=surface, limits=limits)
    adapter.clock[0] = NOW + window + 1
    state, service = services["alice"]
    assert (await admit(service, state, adapter.clock, surface=surface, limits=limits)).phase == "reserved"
    state, service = services["bob"]
    with pytest.raises(QuotaError, match="would be exceeded"):
        await admit(service, state, adapter.clock, owner="bob", surface=surface, limits=limits)


async def test_unsettled_dispatch_remains_a_permanent_hold_under_the_scope(adapter):
    state = adapter.seed("alice", PAST)
    service = scoped(adapter, PAST)
    limits = EntitlementLimits(requestsPerMinute=1)
    record = await admit(service, state, adapter.clock, limits=limits)
    await service.dispatch("alice", record)
    adapter.clock[0] += 2 * MONTH_SECONDS
    assert (await service.reconcile("alice")).entries[record.operationId].phase == "dispatched"
    with pytest.raises(QuotaError, match="would be exceeded"):
        await admit(service, state, adapter.clock, limits=limits)
    assert await admit(service, state, adapter.clock, limits=EntitlementLimits(requestsPerMinute=2))


async def test_attempt_settlement_shape_is_strict_for_every_adapter(adapter):
    state = adapter.seed("alice", PAST)
    service = scoped(adapter, PAST)
    record = await admit(service, state, adapter.clock)
    await service.dispatch("alice", record)
    settled = await service.settle("alice", record, outcome="cancelled")
    snapshot = await adapter.store.read("alice")
    assert QuotaState.model_validate(
        state_document(snapshot.state), context={"persisted_quota": True},
    ) == snapshot.state
    for update in (
        {"charged": settled.charged.model_copy(update={"tokens": 3})},
        {"charged": settled.charged.model_copy(update={"microUsd": 3})},
        {"outcome": None},
    ):
        malformed = snapshot.state.model_copy(update={"entries": {
            record.operationId: settled.model_copy(update=update),
        }})
        inject_accounting(adapter.contract(), snapshot, malformed)
        await assert_invalid_accounting(adapter.contract(), snapshot, malformed)
    inject_accounting(adapter.contract(), snapshot, snapshot.state)
    assert (await adapter.store.read("alice")).state == snapshot.state
    with pytest.raises(ValidationError, match="incomplete known quota settlement"):
        QuotaState.model_validate(snapshot.state.model_copy(update={"entries": {
            record.operationId: settled.model_copy(update={
                "bounds": accounting_bounds(Amounts(tokens=10)),
                "charged": Amounts(tokens=10),
            }),
        }}).model_dump(mode="json"))


@pytest.mark.parametrize("request_count_scope", [True, False], ids=["scope", "historical-control"])
async def test_request_count_retention_bounds_sustained_use(adapter, monkeypatch, request_count_scope):
    from ai4ia_api.hard_quota import models

    # A small budget exercises the same pruning with few round trips.
    monkeypatch.setattr(models, "MAX_STATE_BYTES", 16 * 1024)
    state = adapter.seed("alice", PAST)
    service = scoped(adapter, PAST) if request_count_scope else historical(adapter)
    completed = 0
    for index in range(100):
        adapter.clock[0] = NOW + 120 * index
        try:
            record = await admit(service, state, adapter.clock)
        except QuotaError as exc:
            assert "capacity" in str(exc)
            break
        await service.dispatch("alice", record)
        await service.settle("alice", record, outcome="complete", actual=Amounts())
        completed += 1
    retained = (await adapter.store.read("alice")).state.entries
    if request_count_scope:
        assert completed == 100 and len(retained) <= 4
    else:
        assert completed < 100


async def test_compute_attempts_are_retained_for_their_daily_window(adapter):
    state = adapter.seed("alice", PAST)
    service = scoped(adapter, PAST)
    limits = EntitlementLimits(computeExecutionsPerDay=1)
    record = await admit(service, state, adapter.clock, surface="compute", limits=limits)
    await service.dispatch("alice", record)
    await service.settle("alice", record, outcome="complete", actual=Amounts(compute=1))
    adapter.clock[0] = NOW + DAY_SECONDS - 1
    assert record.operationId in (await service.reconcile("alice")).entries
    with pytest.raises(QuotaError, match="would be exceeded"):
        await admit(service, state, adapter.clock, surface="compute", limits=limits)
    adapter.clock[0] = NOW + DAY_SECONDS + 1
    assert record.operationId not in (await service.reconcile("alice")).entries
    assert await admit(service, state, adapter.clock, surface="compute", limits=limits)


async def test_pruning_never_turns_an_unexpired_identity_into_new_work(adapter):
    state = adapter.seed("alice", PAST)
    service = scoped(adapter, PAST)
    key = operation_id(state.epoch, NOW, "stable")
    record = await admit(service, state, adapter.clock, key=key)
    await service.dispatch("alice", record)
    settled = await service.settle("alice", record, outcome="complete", actual=Amounts())
    # Past the request window but inside the replay horizon: a retry is not new work.
    adapter.clock[0] = NOW + MINUTE_SECONDS + 1
    assert await admit(service, state, adapter.clock, key=key) == settled
    adapter.clock[0] = NOW + REQUEST_COUNT_REPLAY_SECONDS + MINUTE_SECONDS
    assert key not in (await service.reconcile("alice")).entries
    with pytest.raises(QuotaError, match="expired") as caught:
        await admit(service, state, adapter.clock, key=key)
    assert caught.value.code == 409


class NoNumericReads:
    async def window_totals(self, *args, **kwargs):
        raise AssertionError("hard admission must not query the soft ledger")


def controller(adapter: Adapter, scope: RequestCountScope | None) -> AdmissionController:
    return AdmissionController(
        entitlements=EntitlementService(
            InMemoryEntitlementStore(), NoNumericReads(), Entitlement.unlimited(),
            enabled=False, cache_ttl_seconds=0,
        ),
        catalog=ModelCatalog(models=[]),
        pricing=PricingBook({}, currency="USD", version="fixture-v1"),
        store=adapter.store, enabled=True, scope=scope,
    )


async def test_long_context_claims_are_issued_at_their_own_store_time(adapter):
    adapter.seed("alice", PAST)
    admission = controller(adapter, RequestCountScope(PAST))
    with admission_scope(admission, "alice") as context:
        first = await admission.claim(context, "chat", {"turn": 1}, None, None)
        adapter.clock[0] = NOW + REQUEST_COUNT_REPLAY_SECONDS + 1
        second = await admission.claim(context, "chat", {"turn": 2}, None, None)
    assert first is not None and second is not None
    assert parse_operation_id(first.operationId)[1] == NOW
    assert parse_operation_id(second.operationId)[1] == NOW + REQUEST_COUNT_REPLAY_SECONDS + 1
    assert second.phase == "dispatched"
    # Control: an explicitly fixed identity time (the durable replay seam) expires.
    with admission_scope(admission, "alice", issued_at=NOW) as fixed:
        with pytest.raises(QuotaError, match="expired"):
            await admission.claim(fixed, "chat", {"turn": 3}, None, None)


async def test_historical_contexts_keep_one_identity_time(adapter):
    adapter.seed("alice", PAST)
    admission = controller(adapter, None)
    with admission_scope(admission, "alice") as context:
        first = await admission.claim(context, "chat", {"turn": 1}, None, None)
        adapter.clock[0] = NOW + REQUEST_COUNT_REPLAY_SECONDS + 1
        second = await admission.claim(context, "chat", {"turn": 2}, None, None)
    assert first is not None and second is not None
    assert parse_operation_id(first.operationId)[1] == parse_operation_id(second.operationId)[1] == NOW


def test_scope_coverage_must_be_an_exact_bounded_integer():
    for value in (-1, True, 1.0, "1", 2**53):
        with pytest.raises(ValueError):
            RequestCountScope(value)  # type: ignore[arg-type]
    assert RequestCountScope(0).coverage_start == 0


class CountingAccount:
    def __init__(self) -> None:
        self.account = copy.deepcopy(SESSION_ACCOUNT)
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return copy.deepcopy(self.account)


def counting_store(ttl: float, monotonic: list[float]):
    clock = [NOW]
    container = StatefulContainer(lambda: clock[0])
    state = QuotaState(
        userId="alice", epoch=uuid.uuid4().hex, validAfter=PAST, replayFloor=PAST, observedAt=PAST,
    )
    container.seed(state)
    reads = {"container": 0}
    original = container.read

    async def read_container():
        reads["container"] += 1
        return await original()

    container.read = read_container
    account = CountingAccount()
    store = CosmosReservationStore(
        container, read_account=account, layout_ttl_seconds=ttl, clock=lambda: monotonic[0],
    )
    return store, account, reads, clock, state


def scoped_store(store) -> ReservationService:
    return ReservationService(store, scope=RequestCountScope(PAST))


@pytest.mark.parametrize("ttl,expected", [(60, 1), (0, 10)], ids=["bounded", "per-operation-control"])
async def test_layout_metadata_reads_are_bounded_by_the_revalidation_interval(ttl, expected):
    store, account, reads, clock, state = counting_store(ttl, [100.0])
    service = scoped_store(store)
    for _ in range(5):
        await admit(service, state, clock)  # One read and one replace each.
    assert account.calls == reads["container"] == expected


async def test_layout_drift_is_refused_once_the_bounded_interval_elapses():
    monotonic = [100.0]
    store, account, _reads, clock, state = counting_store(60, monotonic)
    service = scoped_store(store)
    assert await admit(service, state, clock)
    account.account["enableMultipleWriteLocations"] = True
    monotonic[0] = 159.0  # Within the documented interval the prior observation stands.
    assert await admit(service, state, clock)
    monotonic[0] = 160.0
    with pytest.raises(QuotaError, match="Session-consistency"):
        await admit(service, state, clock)
    with pytest.raises(QuotaError, match="Session-consistency"):
        await admit(service, state, clock)  # A failed observation is never cached.
    account.account["enableMultipleWriteLocations"] = False
    assert await admit(service, state, clock)


def test_layout_revalidation_interval_is_bounded():
    container = StatefulContainer(lambda: NOW)
    for ttl in (-1, 301):
        with pytest.raises(ValueError):
            CosmosReservationStore(container, read_account=CountingAccount(), layout_ttl_seconds=ttl)
