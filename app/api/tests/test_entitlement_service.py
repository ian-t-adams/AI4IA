"""EntitlementService: the enforcement engine.

Covers the contract that matters for the product requirement "unlimited by
default, but we CAN limit a user": the unlimited fast path does ZERO ledger IO,
disabled fails closed, configured budgets deny with the right code + Retry-After,
ledger errors fail open, and the cache write-through / stale-disabled fallback
behave.
"""
from __future__ import annotations

from datetime import datetime

from ai4ia_api.entitlements.memory_store import InMemoryEntitlementStore
from ai4ia_api.entitlements.models import Entitlement, EntitlementLimits
from ai4ia_api.entitlements.service import EntitlementService
from ai4ia_api.usage.models import WindowTotals


class CountingReader:
    """A UsageWindowReader that records every call so a test can assert the
    unlimited fast path never touches the ledger."""

    def __init__(self, totals: WindowTotals | None = None) -> None:
        self.totals = totals or WindowTotals()
        self.calls = 0

    async def window_totals(self, user_id: str, *, since: datetime, now: datetime | None = None):
        self.calls += 1
        return self.totals


class BoomReader:
    async def window_totals(self, user_id: str, *, since: datetime, now: datetime | None = None):
        raise RuntimeError("ledger down")


def _service(store=None, reader=None, default=None, **kw) -> EntitlementService:
    return EntitlementService(
        store or InMemoryEntitlementStore(),
        reader or CountingReader(),
        default or Entitlement.unlimited("__default__"),
        **kw,
    )


async def test_unlimited_default_allows_with_zero_ledger_io():
    reader = CountingReader()
    svc = _service(reader=reader)
    decision = await svc.check("alice")
    assert decision.allowed is True
    assert reader.calls == 0  # the whole point: unlimited is free


async def test_disabled_user_is_forbidden():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", disabled=True))
    reader = CountingReader()
    svc = _service(store=store, reader=reader)
    decision = await svc.check("u")
    assert decision.allowed is False
    assert decision.code == 403
    assert reader.calls == 0  # disabled short-circuits before any ledger read


async def test_disabled_when_enforcement_disabled_allows():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", disabled=True))
    svc = _service(store=store, enabled=False)
    assert (await svc.check("u")).allowed is True


async def test_rate_limit_denies_with_retry_after():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", requestsPerMinute=3))
    reader = CountingReader(WindowTotals(requests=3))
    svc = _service(store=store, reader=reader)
    decision = await svc.check("u")
    assert decision.allowed is False
    assert decision.code == 429
    assert decision.retry_after_seconds == 60
    assert decision.limit_kind == "requests_per_minute"


async def test_rate_limit_under_cap_allows():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", requestsPerMinute=3))
    reader = CountingReader(WindowTotals(requests=2))
    svc = _service(store=store, reader=reader)
    assert (await svc.check("u")).allowed is True


async def test_tokens_per_day_denies():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", tokensPerDay=1000))
    reader = CountingReader(WindowTotals(totalTokens=1000))
    svc = _service(store=store, reader=reader)
    decision = await svc.check("u")
    assert decision.code == 429
    assert decision.limit_kind == "tokens_per_day"
    assert decision.retry_after_seconds == 24 * 60 * 60


async def test_cost_per_day_denies():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", costPerDayMicroUsd=500))
    reader = CountingReader(WindowTotals(costMicroUsd=750))
    svc = _service(store=store, reader=reader)
    decision = await svc.check("u")
    assert decision.code == 429
    assert decision.limit_kind == "cost_per_day"


async def test_tokens_per_month_denies():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", tokensPerMonth=10_000))
    reader = CountingReader(WindowTotals(totalTokens=10_000))
    svc = _service(store=store, reader=reader)
    decision = await svc.check("u")
    assert decision.code == 429
    assert decision.limit_kind == "tokens_per_month"
    # Retry-After is capped at a day even though the window itself is a month.
    assert decision.retry_after_seconds == 24 * 60 * 60


async def test_cost_per_month_denies():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", costPerMonthMicroUsd=10_000))
    reader = CountingReader(WindowTotals(costMicroUsd=10_001))
    svc = _service(store=store, reader=reader)
    decision = await svc.check("u")
    assert decision.code == 429
    assert decision.limit_kind == "cost_per_month"
    assert decision.retry_after_seconds == 24 * 60 * 60


async def test_zero_limit_is_a_hard_block():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", requestsPerMinute=0))
    reader = CountingReader(WindowTotals(requests=0))
    svc = _service(store=store, reader=reader)
    decision = await svc.check("u")
    assert decision.allowed is False
    assert decision.code == 429


async def test_ledger_error_fails_open():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", tokensPerDay=10))
    svc = _service(store=store, reader=BoomReader())
    assert (await svc.check("u")).allowed is True


async def test_global_default_cap_applies_without_override():
    default = Entitlement(id="__default__", userId="__default__", requestsPerMinute=1)
    reader = CountingReader(WindowTotals(requests=1))
    svc = _service(reader=reader, default=default)
    decision = await svc.check("anyone")
    assert decision.allowed is False
    assert decision.code == 429


async def test_set_writes_through_cache_and_clear_reverts():
    store = InMemoryEntitlementStore()
    svc = _service(store=store, cache_ttl_seconds=300)
    await svc.set("u", EntitlementLimits(disabled=True), updated_by="admin")
    # Effective entitlement reflects the override immediately (same replica).
    eff = await svc.get_effective("u")
    assert eff.disabled is True
    await svc.clear("u")
    eff2 = await svc.get_effective("u")
    assert eff2.is_unlimited is True


async def test_stale_disabled_survives_store_outage():
    class FlakyStore(InMemoryEntitlementStore):
        def __init__(self):
            super().__init__()
            self.fail = False

        async def get(self, user_id):
            if self.fail:
                raise RuntimeError("cosmos blip")
            return await super().get(user_id)

    store = FlakyStore()
    await store.put(Entitlement(id="u", userId="u", disabled=True))
    svc = _service(store=store, cache_ttl_seconds=0)  # force re-read each call
    # Warm the cache with the disabled record.
    assert (await svc.get_effective("u")).disabled is True
    # Now the store fails; the last-known disabled value must still be returned.
    store.fail = True
    eff = await svc.get_effective("u")
    assert eff.disabled is True
    assert (await svc.check("u")).code == 403


async def test_list_overrides_passthrough():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u1", userId="u1", tokensPerDay=1))
    svc = _service(store=store)
    listed = await svc.list_overrides()
    assert [e.userId for e in listed] == ["u1"]


async def test_stale_numeric_limit_fails_open_on_outage():
    """A store outage must NOT resurrect a stale numeric limit (only disabled
    survives) — otherwise a transient blip could wrongly 429 a limited user."""

    class FlakyStore(InMemoryEntitlementStore):
        def __init__(self):
            super().__init__()
            self.fail = False

        async def get(self, user_id):
            if self.fail:
                raise RuntimeError("cosmos blip")
            return await super().get(user_id)

    store = FlakyStore()
    await store.put(Entitlement(id="u", userId="u", requestsPerMinute=0))
    svc = _service(store=store, reader=CountingReader(WindowTotals(requests=99)), cache_ttl_seconds=0)
    # Warm the cache with the limited record (would deny while the store is up).
    assert (await svc.check("u")).code == 429
    # Store now fails: fall open to the unlimited default, not the stale limit.
    store.fail = True
    assert (await svc.check("u")).allowed is True


async def test_ttl_expiry_triggers_reread_and_enforces():
    store = InMemoryEntitlementStore()
    reader = CountingReader(WindowTotals(requests=5))
    svc = _service(store=store, reader=reader, cache_ttl_seconds=0)  # TTL 0 = always re-read
    # Initially unlimited.
    assert (await svc.check("u")).allowed is True
    # Admin sets a limit directly in the store (bypassing the write-through path).
    await store.put(Entitlement(id="u", userId="u", requestsPerMinute=1))
    # With TTL expired, the next check re-reads and enforces.
    assert (await svc.check("u")).code == 429


async def test_budget_windows_use_expected_durations_and_are_separate():
    """RPM/day/month each query the ledger with their own rolling window."""
    seen: list[float] = []

    class RecordingReader:
        async def window_totals(self, user_id, *, since, now=None):
            seen.append((now - since).total_seconds())
            return WindowTotals()  # under all limits so nothing denies

    store = InMemoryEntitlementStore()
    await store.put(
        Entitlement(
            id="u",
            userId="u",
            requestsPerMinute=10,
            tokensPerDay=10,
            tokensPerMonth=10,
        )
    )
    svc = _service(store=store, reader=RecordingReader())
    assert (await svc.check("u")).allowed is True
    assert len(seen) == 3  # three distinct window reads
    assert round(seen[0]) == 60
    assert round(seen[1]) == 24 * 60 * 60
    assert round(seen[2]) == 30 * 24 * 60 * 60


async def test_daily_token_and_cost_share_one_window_read():
    """Both daily dimensions are covered by a single 24h ledger read."""
    calls = CountingReader(WindowTotals())
    store = InMemoryEntitlementStore()
    await store.put(
        Entitlement(id="u", userId="u", tokensPerDay=100, costPerDayMicroUsd=100)
    )
    svc = _service(store=store, reader=calls)
    await svc.check("u")
    assert calls.calls == 1

