"""Owner-partition records and the ledger: strict limits, idempotent removal."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosResourceNotFoundError

from ai4ia_api.photo_avatars.store import (
    LEDGER_ID,
    LimitReached,
    PhotoAvatarRecord,
    PhotoAvatarReport,
    PhotoAvatarStore,
)
from ai4ia_api.publishing.store import CosmosRecordStore, InMemoryRecordStore

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def record(owner: str = "alice", index: int = 0, **extra) -> PhotoAvatarRecord:
    values = dict(
        id=f"{index:032x}", userId=owner, providerAvatarId=f"ai4ia-{index:020x}", homeRegion="eastus2",
        displayName="Host", prompt="A host.", attributes={"gender": None}, attestationVersion="v",
        attestedAt=NOW, status="creating", createdAt=NOW, updatedAt=NOW,
    )
    values.update(extra)
    return PhotoAvatarRecord(**values)


class YieldingRecords(InMemoryRecordStore):
    """Real awaits between read and conditional write, so writers interleave."""

    async def read(self, owner, identifier):
        value = await super().read(owner, identifier)
        await asyncio.sleep(0)
        return value


async def test_both_limits_hold_and_old_creations_age_out():
    store = PhotoAvatarStore(InMemoryRecordStore())
    for index in range(3):
        await store.reserve(record(index=index), max_avatars=3, max_per_day=5, now=NOW)
    with pytest.raises(LimitReached) as full:
        await store.reserve(record(index=9), max_avatars=3, max_per_day=5, now=NOW)
    assert full.value.kind == "max_avatars"
    await store.remove("alice", record(index=0).id)
    await store.remove("alice", record(index=1).id)
    # Deleting frees avatar slots, never daily creations.
    await store.reserve(record(index=3), max_avatars=3, max_per_day=4, now=NOW)
    with pytest.raises(LimitReached) as daily:
        await store.reserve(record(index=4), max_avatars=3, max_per_day=4, now=NOW + timedelta(hours=1))
    assert daily.value.kind == "daily"
    assert daily.value.retry_after == int(timedelta(hours=23).total_seconds()) + 1
    await store.reserve(record(index=4), max_avatars=3, max_per_day=4, now=NOW + timedelta(days=1, seconds=1))
    ledger = await store.ledger("alice")
    assert len(ledger.creations) == 1  # pruned to the rolling window on write


async def test_concurrent_creates_cannot_exceed_either_limit():
    store = PhotoAvatarStore(YieldingRecords())
    results = await asyncio.gather(
        *(store.reserve(record(index=i), max_avatars=5, max_per_day=50, now=NOW) for i in range(12)),
        return_exceptions=True,
    )
    reserved = [result for result in results if result is None]
    assert len(reserved) == 5
    assert all(isinstance(result, (LimitReached, Exception)) for result in results if result is not None)
    ledger = await store.ledger("alice")
    assert len(ledger.active) == 5 == len(await store.list("alice"))
    # Control: the same interleaving within the limit reserves every writer.
    roomy = PhotoAvatarStore(YieldingRecords())
    outcomes = await asyncio.gather(
        *(roomy.reserve(record(index=i), max_avatars=50, max_per_day=50, now=NOW) for i in range(6)),
        return_exceptions=True,
    )
    assert outcomes == [None] * 6
    assert len(await roomy.list("alice")) == 6 == len((await roomy.ledger("alice")).active)


async def test_release_restores_the_slot_and_the_daily_creation():
    store = PhotoAvatarStore(InMemoryRecordStore())
    await store.reserve(record(index=1), max_avatars=1, max_per_day=1, now=NOW)
    await store.release("alice", record(index=1).id, created_at=NOW)
    assert await store.get("alice", record(index=1).id) is None
    ledger = await store.ledger("alice")
    assert ledger.active == [] and ledger.creations == []
    await store.reserve(record(index=2), max_avatars=1, max_per_day=1, now=NOW)


async def test_records_are_owner_scoped_and_removal_is_idempotent():
    store = PhotoAvatarStore(InMemoryRecordStore())
    await store.reserve(record(index=1), max_avatars=5, max_per_day=5, now=NOW)
    assert await store.get("bob", record(index=1).id) is None
    assert await store.list("bob") == []
    assert (await store.get("alice", record(index=1).id))[0].userId == "alice"
    await store.remove("bob", record(index=1).id)
    assert await store.get("alice", record(index=1).id) is not None
    await store.remove("alice", record(index=1).id)
    await store.remove("alice", record(index=1).id)
    assert await store.get("alice", record(index=1).id) is None
    assert (await store.ledger("alice")).active == []


async def test_reports_have_their_own_rolling_cap():
    store = PhotoAvatarStore(InMemoryRecordStore())
    for index in range(3):
        await store.add_report(
            PhotoAvatarReport(id=f"report-{index}", userId="alice", avatarId="a" * 32, reason="other",
                              createdAt=NOW),
            max_per_day=3, now=NOW,
        )
    with pytest.raises(LimitReached) as capped:
        await store.add_report(
            PhotoAvatarReport(id="report-x", userId="alice", avatarId="a" * 32, reason="other", createdAt=NOW),
            max_per_day=3, now=NOW + timedelta(hours=2),
        )
    assert capped.value.kind == "reports" and capped.value.retry_after == 22 * 3600 + 1


class LedgerContainer:
    """A Cosmos container fake whose batches consume their conditional options."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.race = False
        self.batches: list[list] = []

    async def read_item(self, *, item, partition_key):
        value = self.items.get(item)
        if value is None or value["userId"] != partition_key:
            raise CosmosResourceNotFoundError(message="missing")
        return copy.deepcopy(value)

    async def read(self):
        return {"id": "photoAvatars"}

    async def execute_item_batch(self, *, batch_operations, partition_key):
        self.batches.append(batch_operations)
        if self.race and LEDGER_ID in self.items:
            self.race = False
            self.items[LEDGER_ID]["_etag"] = "raced"
        pending = copy.deepcopy(self.items)
        for index, operation in enumerate(batch_operations):
            verb, args, *extras = operation
            options = extras[0] if extras else {}
            etag = options.pop("if_match_etag", None)  # consumed like the SDK
            identifier = args[0]["id"] if verb == "create" else args[0]
            current = pending.get(identifier)
            failure = (
                409 if verb == "create" and current is not None else
                412 if verb != "create" and (current is None or current["_etag"] != etag) else None
            )
            if failure is not None:
                raise CosmosBatchOperationError(
                    error_index=index, status_code=failure, headers={}, message="conflict",
                    operation_responses=[{"statusCode": failure}],
                )
            if verb == "delete":
                pending.pop(identifier)
            else:
                body = args[0] if verb == "create" else args[1]
                pending[identifier] = {**copy.deepcopy(body), "_etag": f"etag-{len(self.batches)}"}
        self.items = pending


async def test_cosmos_retries_resend_fresh_conditional_options_after_a_lost_race():
    container = LedgerContainer()
    store = PhotoAvatarStore(CosmosRecordStore(container))
    await store.reserve(record(index=1), max_avatars=5, max_per_day=5, now=NOW)
    container.race = True
    await store.reserve(record(index=2), max_avatars=5, max_per_day=5, now=NOW)
    first, second = container.batches[-2:]
    # The retry rebuilt its batch; the consumed options of the lost attempt are not reused.
    assert first is not second
    ledger_ops = [op for op in second if op[0] == "replace" and op[1][0] == LEDGER_ID]
    assert ledger_ops and ledger_ops[0][2] == {}  # consumed by the fake after sending
    assert container.items[LEDGER_ID]["active"] == [record(index=1).id, record(index=2).id]
