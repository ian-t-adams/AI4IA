"""Cosmos ``query_records`` / ``query_rollup_rows`` construction + memory parity.

The admin aggregation reads the ledger cross-partition through ``query_records``
(full rows) or ``query_rollup_rows`` (the projected rows the consolidated
overview uses). This asserts both repos issue a bounded (``TOP``), time-windowed,
newest-first query, that the projected one names only the fields the rollups read
rather than ``SELECT *``, and that both marshal rows correctly — and degrade to an
empty list when the container is absent — without any live Cosmos. A fake
container captures the query/params and yields preset rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from azure.cosmos.exceptions import CosmosResourceNotFoundError

from ai4ia_api.usage.cosmos_repo import CosmosUsageRepository
from ai4ia_api.usage.memory_repo import InMemoryUsageRepository
from ai4ia_api.usage.models import ROLLUP_FIELDS, UsageRecord, UsageRollupRow

NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=30)


def _rec(user: str = "alice", **kw) -> UsageRecord:
    base = dict(
        userId=user,
        sessionId="s1",
        model="gpt-5.2",
        deployment="dep",
        totalTokens=15,
        createdAt=NOW,
    )
    base.update(kw)
    return UsageRecord(**base)


class _QueryUsage:
    """Minimal async stand-in for the Cosmos ``usage`` container client."""

    def __init__(self, rows: list[dict], *, raise_not_found: bool = False) -> None:
        self._rows = rows
        self._raise = raise_not_found
        self.last_query: str | None = None
        self.last_params: list | None = None

    async def query_items(self, *, query, parameters=None):
        self.last_query = query
        self.last_params = parameters
        if self._raise:
            raise CosmosResourceNotFoundError(message="missing container")
        for row in self._rows:
            yield row


def _repo(rows: list[dict], *, raise_not_found: bool = False):
    repo = object.__new__(CosmosUsageRepository)
    fake = _QueryUsage(rows, raise_not_found=raise_not_found)
    repo._usage = fake
    return repo, fake


async def test_query_records_marshals_rows():
    rows = [_rec("alice").model_dump(mode="json"), _rec("bob").model_dump(mode="json")]
    repo, fake = _repo(rows)
    out = await repo.query_records(since=SINCE, now=NOW, limit=100)
    assert [r.userId for r in out] == ["alice", "bob"]
    assert all(isinstance(r, UsageRecord) for r in out)


async def test_query_records_defaults_old_rows_without_provider_or_deployment():
    rows = [
        {
            "userId": "alice",
            "sessionId": "s1",
            "model": "gpt-5.2",
            "createdAt": NOW.isoformat(),
        }
    ]
    repo, fake = _repo(rows)
    out = await repo.query_records(since=SINCE, now=NOW, limit=100)
    assert out[0].provider == "azure_openai"
    assert out[0].deployment is None
    assert out[0].target is None


async def test_query_records_query_is_bounded_and_windowed():
    repo, fake = _repo([])
    await repo.query_records(since=SINCE, now=NOW, limit=250)
    assert "SELECT TOP 250" in fake.last_query
    assert "c.createdAt >= @since" in fake.last_query
    assert "c.createdAt <= @now" in fake.last_query
    assert "ORDER BY c.createdAt DESC" in fake.last_query
    by_name = {p["name"]: p["value"] for p in fake.last_params}
    assert by_name["@since"] == SINCE.isoformat()
    assert by_name["@now"] == NOW.isoformat()


async def test_query_records_missing_container_degrades_to_empty():
    repo, fake = _repo([], raise_not_found=True)
    assert await repo.query_records(since=SINCE, now=NOW, limit=100) == []


# ---- projected rollup scan (admin overview) ----


async def test_query_rollup_rows_projects_instead_of_selecting_star():
    """The admin overview must not pull ten unread fields per row at the cap."""
    repo, fake = _repo([])
    await repo.query_rollup_rows(since=SINCE, now=NOW, limit=250)
    assert "SELECT TOP 250" in fake.last_query
    assert "*" not in fake.last_query
    for field in ROLLUP_FIELDS:
        assert f"c.{field}" in fake.last_query
    # Fields no rollup reads must NOT be fetched.
    for unread in ("c.sessionId", "c.correlationId", "c.priceVersion", "c.calls"):
        assert unread not in fake.last_query
    # Same window + ordering contract as the full scan.
    assert "c.createdAt >= @since" in fake.last_query
    assert "c.createdAt <= @now" in fake.last_query
    assert "ORDER BY c.createdAt DESC" in fake.last_query
    by_name = {p["name"]: p["value"] for p in fake.last_params}
    assert by_name["@since"] == SINCE.isoformat()
    assert by_name["@now"] == NOW.isoformat()


async def test_query_rollup_rows_marshals_projected_documents():
    docs = [_rec("alice").model_dump(mode="json"), _rec("bob").model_dump(mode="json")]
    rows = [{k: doc[k] for k in ROLLUP_FIELDS if k in doc} for doc in docs]
    repo, fake = _repo(rows)
    out = await repo.query_rollup_rows(since=SINCE, now=NOW, limit=100)
    assert [r.userId for r in out] == ["alice", "bob"]
    assert all(isinstance(r, UsageRollupRow) for r in out)
    # Identical values to the full-record path for the same documents.
    assert out == [UsageRollupRow.from_record(UsageRecord.model_validate(d)) for d in docs]


async def test_query_rollup_rows_missing_container_degrades_to_empty():
    repo, fake = _repo([], raise_not_found=True)
    assert await repo.query_rollup_rows(since=SINCE, now=NOW, limit=100) == []


async def test_memory_query_rollup_rows_matches_the_full_scan_row_for_row():
    repo = InMemoryUsageRepository()
    await repo.record(_rec("a", createdAt=NOW))
    await repo.record(_rec("b", createdAt=NOW - timedelta(days=1)))
    await repo.record(_rec("c", createdAt=NOW - timedelta(days=120)))
    records = await repo.query_records(since=SINCE, now=NOW, limit=100)
    rows = await repo.query_rollup_rows(since=SINCE, now=NOW, limit=100)
    assert rows == [UsageRollupRow.from_record(r) for r in records]
    # Cap and newest-first ordering carry over unchanged.
    capped = await repo.query_rollup_rows(since=SINCE, now=NOW, limit=1)
    assert [r.userId for r in capped] == ["a"]


async def test_memory_query_records_window_and_cap_parity():
    repo = InMemoryUsageRepository()
    await repo.record(_rec("a", createdAt=NOW))
    await repo.record(_rec("b", createdAt=NOW - timedelta(days=1)))
    # Outside the window -> excluded.
    await repo.record(_rec("c", createdAt=NOW - timedelta(days=120)))
    out = await repo.query_records(since=SINCE, now=NOW, limit=100)
    assert {r.userId for r in out} == {"a", "b"}
    # Newest first.
    assert out[0].userId == "a"
    # Cap honoured.
    capped = await repo.query_records(since=SINCE, now=NOW, limit=1)
    assert len(capped) == 1
    assert capped[0].userId == "a"
