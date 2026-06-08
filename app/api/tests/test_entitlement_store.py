"""InMemoryEntitlementStore CRUD behavior."""
from __future__ import annotations

import pytest

from ai4ia_api.entitlements.memory_store import InMemoryEntitlementStore
from ai4ia_api.entitlements.models import Entitlement


@pytest.fixture
def store() -> InMemoryEntitlementStore:
    return InMemoryEntitlementStore()


async def test_get_missing_returns_none(store):
    assert await store.get("nobody") is None


async def test_put_then_get_roundtrip(store):
    ent = Entitlement(id="u1", userId="u1", tokensPerDay=100)
    await store.put(ent)
    fetched = await store.get("u1")
    assert fetched is not None
    assert fetched.userId == "u1"
    assert fetched.tokensPerDay == 100


async def test_put_overwrites(store):
    await store.put(Entitlement(id="u1", userId="u1", tokensPerDay=100))
    await store.put(Entitlement(id="u1", userId="u1", tokensPerDay=200))
    fetched = await store.get("u1")
    assert fetched.tokensPerDay == 200


async def test_delete_removes(store):
    await store.put(Entitlement(id="u1", userId="u1", disabled=True))
    await store.delete("u1")
    assert await store.get("u1") is None


async def test_delete_missing_is_noop(store):
    await store.delete("ghost")  # must not raise


async def test_list_returns_all_overrides(store):
    await store.put(Entitlement(id="u1", userId="u1", tokensPerDay=1))
    await store.put(Entitlement(id="u2", userId="u2", disabled=True))
    listed = await store.list()
    assert {e.userId for e in listed} == {"u1", "u2"}
