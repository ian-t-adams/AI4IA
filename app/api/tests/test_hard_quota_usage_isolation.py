"""All Cosmos usage readers exclude coordination records without losing legacy usage."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone

import pytest

from ai4ia_api.hard_quota.models import STATE_ID, STATE_KIND
from ai4ia_api.hard_quota.store import LocalReservationStore
from ai4ia_api.usage.cosmos_repo import CosmosUsageRepository
from ai4ia_api.usage.models import UsageRecord

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=1)


class MixedOwnerPartition:
    def __init__(self, *, damaged_kind=False):
        self.rows = []
        for owner in ("alice", "bob"):
            legacy = UsageRecord(
                id=f"usage-{owner}", userId=owner, sessionId="session",
                model="model", createdAt=NOW - timedelta(seconds=1), usageKnown=True, totalTokens=5,
            ).model_dump(mode="json")
            self.rows.append(legacy)
            state = LocalReservationStore(clock=NOW.timestamp).seed(owner).model_dump(mode="json")
            # These fields deliberately make a coordination row eligible for
            # legacy scans: relying on its current lack of createdAt is unsafe.
            state.update(createdAt=NOW.isoformat(), sessionId="session", model="state-not-usage")
            # Damaged/future records must still be excluded by either marker.
            self.rows.append({**state, "kind": "damaged"} if damaged_kind else state)
            self.rows.append({**state, "id": "quota-future-id"})

    async def query_items(self, *, query, parameters):
        values = {entry["name"]: entry["value"] for entry in parameters}
        rows = copy.deepcopy(self.rows)
        if f"c.id != '{STATE_ID}'" in query:
            rows = [row for row in rows if row["id"] != STATE_ID]
        if f"c.kind != '{STATE_KIND}'" in query:
            rows = [row for row in rows if row.get("kind") != STATE_KIND]
        if "@uid" in query:
            rows = [row for row in rows if row["userId"] == values["@uid"]]
        if "@sid" in query:
            rows = [row for row in rows if row.get("sessionId") == values["@sid"]]
        if "@since" in query:
            rows = [row for row in rows if row.get("createdAt", "") >= values["@since"]]
        if "@now" in query:
            rows = [row for row in rows if row.get("createdAt", "") <= values["@now"]]
        rows.sort(key=lambda row: row.get("createdAt", ""), reverse=True)
        top = re.search(r"SELECT TOP (\d+)", query)
        if top:
            rows = rows[:int(top[1])]
        for row in rows:
            yield row


@pytest.mark.parametrize("damaged_kind", [False, True])
async def test_every_usage_reader_discriminates_same_partition_coordination_rows(damaged_kind):
    fake = MixedOwnerPartition(damaged_kind=damaged_kind)
    before = copy.deepcopy(fake.rows)
    repo = object.__new__(CosmosUsageRepository)
    repo._usage = fake
    summary = await repo.summarize("alice", since=SINCE, since_days=1, now=NOW)
    assert summary.totalRequests == 1
    assert summary.totalTokens == 5
    records = await repo.query_records(since=SINCE, now=NOW, limit=100)
    assert [record.id for record in records] == ["usage-alice", "usage-bob"]
    rollups = await repo.query_rollup_rows(since=SINCE, now=NOW, limit=100)
    assert [row.userId for row in rollups] == ["alice", "bob"]
    session = await repo.list_for_session("alice", "session", limit=100)
    assert [record.id for record in session] == ["usage-alice"]
    assert fake.rows == before

    # Positive control: the exact fake/query path can return those records when
    # discrimination is absent. This proves absence is not a preset fake answer.
    unfiltered = [
        row async for row in fake.query_items(
            query="SELECT * FROM c WHERE c.userId = @uid",
            parameters=[{"name": "@uid", "value": "alice"}],
        )
    ]
    assert len(unfiltered) == 3
    assert any(row["id"] == STATE_ID for row in unfiltered)
