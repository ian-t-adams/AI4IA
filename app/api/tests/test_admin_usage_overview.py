"""The consolidated admin overview: one scan, same numbers, far less memory.

Audit P1-15: the dashboard fired seven usage reports concurrently, and each one
independently pulled up to ``MAX_ADMIN_RECORDS`` *full* ledger rows for the same
window. A read-only microbenchmark put one 50,000-row ``UsageRecord`` list at
~87 MiB, so seven concurrent lists imply ~610 MiB — against a 1 GiB API replica
that is also serving chat.

This module holds the three things that finding needs closed:

1. **One scan.** ``overview`` reads the ledger exactly once where the seven
   single-panel reports read it seven times.
2. **Same numbers.** Every section of the overview equals what its single-panel
   endpoint returns for the same ledger — including the ``usageKnown`` /
   ``costKnown`` honesty flags, which must never collapse into a plain zero.
3. **A measured win, not a claim.** ``test_projected_rollup_row_is_materially_
   smaller_than_a_full_record`` and its companion measure real retained bytes
   and assert the improvement, printing the figures so the numbers in the PR
   have a source.
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import pytest

from ai4ia_api.usage.aggregate import (
    MAX_ADMIN_RECORDS,
    AdminUsageService,
    aggregate_agents,
    aggregate_by_data_zone,
    aggregate_by_day,
    aggregate_by_deployment,
    aggregate_by_model,
    aggregate_by_provider,
    aggregate_by_region,
    aggregate_by_status,
    aggregate_by_user,
    aggregate_summary,
    aggregate_user_agents,
)
from ai4ia_api.usage.memory_repo import InMemoryUsageRepository
from ai4ia_api.usage.models import ROLLUP_FIELDS, UsageRecord, UsageRollupRow

NOW = datetime.now(timezone.utc) - timedelta(minutes=1)
#: A fixed instant for parsing assertions that compare literal ISO strings.
FIXED = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def rec(user: str = "alice", **kw) -> UsageRecord:
    base = dict(
        userId=user,
        sessionId="s1",
        provider="azure_openai",
        model="gpt-5.2",
        deployment="dep-a",
        target="dep-a",
        region="eastus",
        dataZone="us",
        agent=None,
        status="complete",
        billable=True,
        usageKnown=True,
        usageComplete=True,
        calls=1,
        promptTokens=10,
        completionTokens=5,
        totalTokens=15,
        costKnown=True,
        estCostMicroUsd=1000,
        currency="USD",
        priceInputPer1M=2.5,
        priceOutputPer1M=10.0,
        priceVersion="2024-06",
        correlationId="corr-0123456789abcdef",
        createdAt=NOW,
    )
    base.update(kw)
    return UsageRecord(**base)


LEDGER = [
    rec("alice", agent="research", createdAt=NOW),
    rec("alice", agent="coder", model="o3", createdAt=NOW - timedelta(days=1)),
    rec("bob", agent="research", region="westus", dataZone=None, createdAt=NOW),
    # Usage not reported: tokens must NOT be summed, and it is not billable-known.
    rec(
        "bob",
        usageKnown=False,
        usageComplete=False,
        promptTokens=None,
        completionTokens=None,
        totalTokens=None,
        costKnown=False,
        estCostMicroUsd=None,
        createdAt=NOW - timedelta(days=2),
    ),
    # Billable but unpriced: costKnown must go False, never a silent zero.
    rec("carol", costKnown=False, estCostMicroUsd=None, createdAt=NOW),
    rec("carol", status="error", billable=False, createdAt=NOW),
    rec("carol", status="cancelled", billable=False, deployment=None, createdAt=NOW),
]


async def _seeded_repo(records: list[UsageRecord] | None = None) -> InMemoryUsageRepository:
    repo = InMemoryUsageRepository()
    for record in records if records is not None else LEDGER:
        await repo.record(record)
    return repo


class _CountingRepo:
    """Wraps the in-memory ledger and counts each kind of bounded scan."""

    def __init__(self, inner: InMemoryUsageRepository) -> None:
        self._inner = inner
        self.full_scans = 0
        self.projected_scans = 0

    async def query_records(self, *, since, now, limit):
        self.full_scans += 1
        return await self._inner.query_records(since=since, now=now, limit=limit)

    async def query_rollup_rows(self, *, since, now, limit):
        self.projected_scans += 1
        return await self._inner.query_rollup_rows(since=since, now=now, limit=limit)


class _LegacyRepo:
    """A repository that predates ``query_rollup_rows`` (e.g. an injected double)."""

    def __init__(self, inner: InMemoryUsageRepository) -> None:
        self._inner = inner
        self.full_scans = 0

    async def query_records(self, *, since, now, limit):
        self.full_scans += 1
        return await self._inner.query_records(since=since, now=now, limit=limit)


# ---- 1. one scan instead of seven ----


async def test_overview_reads_the_ledger_once_where_the_fan_out_reads_it_seven_times():
    repo = _CountingRepo(await _seeded_repo())
    service = AdminUsageService(repo)

    await service.overview(days=30)
    assert (repo.projected_scans, repo.full_scans) == (1, 0)

    # The seven single-panel reports the dashboard used to fire concurrently.
    await service.summary(days=30)
    await service.by_model(days=30)
    await service.by_day(days=30)
    await service.by_user(days=30)
    await service.agents(days=30)
    await service.user_agents(days=30)
    await service.distributions(days=30)
    assert repo.full_scans == 7


async def test_overview_prefers_the_projected_scan_but_falls_back_to_full_records():
    """The projection is an optimization, not a repository requirement."""
    repo = _LegacyRepo(await _seeded_repo())
    report = await AdminUsageService(repo).overview(days=30)
    assert repo.full_scans == 1
    assert report.scannedRecords == len(LEDGER)
    assert report.summary.activeUsers == 3


# ---- 2. identical numbers to the seven endpoints ----


async def test_overview_matches_every_single_panel_report_for_the_same_window():
    service = AdminUsageService(await _seeded_repo())
    overview = await service.overview(days=30)

    summary = await service.summary(days=30)
    by_model = await service.by_model(days=30)
    by_day = await service.by_day(days=30)
    by_user = await service.by_user(days=30)
    agents = await service.agents(days=30)
    user_agents = await service.user_agents(days=30)
    distributions = await service.distributions(days=30)

    # Window metadata is identical apart from the wall-clock stamps.
    assert overview.sinceDays == summary.sinceDays == 30
    assert overview.truncated is summary.truncated is False
    assert overview.scannedRecords == summary.scannedRecords == len(LEDGER)

    ignore = {"fromTime", "toTime"}
    assert overview.summary.model_dump(exclude=ignore) == summary.model_dump(exclude=ignore)
    assert overview.byModel == by_model.byModel
    assert overview.byDay == by_day.byDay
    assert overview.byUser == by_user.byUser
    assert overview.totalUsers == by_user.totalUsers
    assert overview.userLimit == by_user.limit
    assert overview.userOffset == by_user.offset
    assert overview.agents == agents.agents
    assert overview.userAgents == user_agents.userAgents
    assert overview.byRegion == distributions.byRegion
    assert overview.byDataZone == distributions.byDataZone
    assert overview.byProvider == distributions.byProvider
    assert overview.byDeployment == distributions.byDeployment
    assert overview.byStatus == distributions.byStatus
    assert overview.partialSections == []


async def test_overview_preserves_the_unknown_cost_and_usage_distinction():
    """Unknown must stay unknown: no rollup may report it as a zero."""
    overview = await AdminUsageService(await _seeded_repo()).overview(days=30)

    # One turn reported no usage at all -> counted, but its tokens are not summed.
    assert overview.summary.unknownUsageRequests == 1
    assert overview.summary.totalTokens == 15 * 6
    # One billable turn had no price -> flagged, and its cost is not counted as 0.
    assert overview.summary.costUnknownRequests == 2
    assert overview.summary.totalCostMicroUsd == 1000 * 5

    carol = next(bucket for bucket in overview.byUser if bucket.userId == "carol")
    # Carol has one unpriced billable turn plus two priced ones: the subtotal is
    # real, and costKnown=False is the signal that it is only a subtotal.
    assert carol.costKnown is False
    assert carol.costMicroUsd == 2000

    # The dimension rollups carry the same flag rather than flattening it.
    eastus = next(bucket for bucket in overview.byRegion if bucket.key == "eastus")
    assert eastus.costKnown is False
    unknown_zone = next(bucket for bucket in overview.byDataZone if bucket.key == "(unknown)")
    assert unknown_zone.requests == 1


async def test_overview_paginates_users_and_reports_the_full_population():
    ledger = [rec(f"user-{i}", totalTokens=100 - i) for i in range(5)]
    service = AdminUsageService(await _seeded_repo(ledger))

    page = await service.overview(days=30, user_limit=2, user_offset=1)
    assert page.totalUsers == 5
    assert page.userLimit == 2
    assert page.userOffset == 1
    assert [bucket.userId for bucket in page.byUser] == ["user-1", "user-2"]

    # Out-of-range page: empty rows, honest population count (never an error).
    beyond = await service.overview(days=30, user_limit=20, user_offset=99)
    assert beyond.byUser == []
    assert beyond.totalUsers == 5


async def test_overview_marks_truncation_when_the_record_cap_is_hit():
    service = AdminUsageService(await _seeded_repo(), max_records=3)
    report = await service.overview(days=30)
    assert report.truncated is True
    assert report.scannedRecords == 3


async def test_one_failing_rollup_degrades_only_its_own_section(monkeypatch):
    """Consolidation must not turn seven independent panels into all-or-nothing."""
    monkeypatch.setattr(
        "ai4ia_api.usage.aggregate.aggregate_agents",
        lambda records: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    report = await AdminUsageService(await _seeded_repo()).overview(days=30)

    assert report.partialSections == ["agents"]
    assert report.agents == []
    # Every other section still carries real data.
    assert report.summary.totalRequests == len(LEDGER)
    assert report.byModel and report.byDay and report.byUser
    assert report.userAgents and report.byRegion and report.byStatus


# ---- projection accuracy: the slim row must not change any number ----


def test_rollup_projection_covers_exactly_the_fields_the_aggregates_read():
    """The projected field list is the contract; drift here silently zeroes data."""
    assert set(ROLLUP_FIELDS) == {
        "userId",
        "model",
        "provider",
        "status",
        "billable",
        "usageKnown",
        "costKnown",
        "createdAt",
        "agent",
        "deployment",
        "region",
        "dataZone",
        "promptTokens",
        "completionTokens",
        "totalTokens",
        "estCostMicroUsd",
    }
    # Every projected name is a real ledger field (no typo can survive).
    assert set(ROLLUP_FIELDS) <= set(UsageRecord.model_fields)
    # …and the projection really is a projection: the ledger carries more.
    assert set(UsageRecord.model_fields) - set(ROLLUP_FIELDS)


@pytest.mark.parametrize(
    "aggregate",
    [
        aggregate_summary,
        aggregate_by_model,
        aggregate_by_day,
        aggregate_by_user,
        aggregate_agents,
        aggregate_user_agents,
        aggregate_by_region,
        aggregate_by_data_zone,
        aggregate_by_provider,
        aggregate_by_deployment,
        aggregate_by_status,
    ],
)
def test_every_aggregate_is_identical_over_projected_rows(aggregate):
    projected = [UsageRollupRow.from_record(record) for record in LEDGER]
    full_result = aggregate(LEDGER)
    projected_result = aggregate(projected)
    if isinstance(full_result, list):
        assert [item.model_dump() for item in projected_result] == [
            item.model_dump() for item in full_result
        ]
    else:
        ignore = {"fromTime", "toTime"}
        assert projected_result.model_dump(exclude=ignore) == full_result.model_dump(
            exclude=ignore
        )


def test_projected_row_parses_a_stored_document_the_same_way_the_full_record_does():
    """``from_document`` is the Cosmos path; it must agree with pydantic exactly."""
    for record in LEDGER:
        doc = record.model_dump(mode="json")
        projected = {key: doc[key] for key in ROLLUP_FIELDS if key in doc}
        row = UsageRollupRow.from_document(projected)
        assert row == UsageRollupRow.from_record(UsageRecord.model_validate(doc))


def test_projected_row_defaults_old_documents_without_optional_fields():
    """A projection omits keys the document never had; defaults must match."""
    row = UsageRollupRow.from_document(
        {"userId": "alice", "model": "gpt-5.2", "createdAt": NOW.isoformat()}
    )
    reference = UsageRollupRow.from_record(
        UsageRecord.model_validate(
            {"userId": "alice", "sessionId": "s1", "model": "gpt-5.2", "createdAt": NOW.isoformat()}
        )
    )
    assert row == reference
    assert row.provider == "azure_openai"
    assert (row.agent, row.deployment, row.region, row.dataZone) == (None, None, None, None)
    assert (row.usageKnown, row.costKnown, row.billable) == (False, False, False)
    # Absent token counts stay None (unknown), never 0.
    assert (row.promptTokens, row.completionTokens, row.totalTokens) == (None, None, None)
    assert row.estCostMicroUsd is None


@pytest.mark.parametrize(
    "stored",
    [
        "2024-06-15T12:00:00Z",
        "2024-06-15T12:00:00+00:00",
        "2024-06-15T12:00:00.123456Z",
        "2024-06-15T14:00:00+02:00",
        "2024-06-15T12:00:00",  # naive -> treated as UTC, like every ledger write
    ],
)
def test_projected_row_parses_created_at_like_pydantic(stored):
    row = UsageRollupRow.from_document(
        {"userId": "a", "model": "m", "createdAt": stored}
    )
    expected = UsageRecord.model_validate(
        {"userId": "a", "sessionId": "s", "model": "m", "createdAt": stored}
    ).createdAt
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=timezone.utc)
    assert row.createdAt == expected


def test_projected_row_refuses_an_unparseable_timestamp():
    """A corrupt row fails loudly rather than landing in the wrong day bucket."""
    with pytest.raises(ValueError):
        UsageRollupRow.from_document({"userId": "a", "model": "m", "createdAt": None})


# ---- 3. the measured win ----


def deep_sizeof(obj: object, seen: set[int] | None = None) -> int:
    """Bytes retained by ``obj`` and everything it uniquely owns.

    Deterministic (unlike RSS or ``tracemalloc``, which vary with allocator and
    GC timing), so it is safe to assert on in CI. Shared/interned objects are
    counted once — which is what a single resident list actually costs.
    """
    seen = set() if seen is None else seen
    if id(obj) in seen:
        return 0
    seen.add(id(obj))
    size = sys.getsizeof(obj)
    if isinstance(obj, (str, bytes, bytearray, int, float, bool, datetime)) or obj is None:
        return size
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            size += deep_sizeof(key, seen) + deep_sizeof(value, seen)
        return size
    if isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            size += deep_sizeof(item, seen)
        return size
    instance_dict = getattr(obj, "__dict__", None)
    if instance_dict is not None:
        size += deep_sizeof(instance_dict, seen)
    fields_set = getattr(obj, "__pydantic_fields_set__", None)
    if fields_set is not None:
        size += deep_sizeof(fields_set, seen)
    for slot in getattr(type(obj), "__slots__", ()) or ():
        try:
            size += deep_sizeof(getattr(obj, slot), seen)
        except AttributeError:
            continue
    return size


#: Sample size for the memory measurements. Big enough for a stable per-row
#: figure, small enough to keep the suite fast; results are reported per row and
#: extrapolated to the real ``MAX_ADMIN_RECORDS`` cap.
SAMPLE_ROWS = 2_000
_MIB = 1024 * 1024


def _sample_documents(count: int) -> list[dict]:
    """Realistic stored ledger documents with per-row unique strings.

    Ids/sessions/correlation ids are unique per row exactly as they are in
    production, so the measurement is not flattered by string interning.
    """
    return [
        rec(
            f"user-{index % 50:04d}",
            sessionId=f"session-{index:08d}",
            correlationId=f"corr-{index:08d}-0123456789abcdef",
            agent="research" if index % 3 else None,
            createdAt=NOW - timedelta(minutes=index),
        ).model_dump(mode="json")
        for index in range(count)
    ]


def test_projected_rollup_row_is_materially_smaller_than_a_full_record(capsys):
    docs = _sample_documents(SAMPLE_ROWS)
    full = [UsageRecord.model_validate(doc) for doc in docs]
    projected = [
        UsageRollupRow.from_document({k: doc[k] for k in ROLLUP_FIELDS if k in doc})
        for doc in docs
    ]

    full_bytes = deep_sizeof(full)
    projected_bytes = deep_sizeof(projected)
    full_per_row = full_bytes / SAMPLE_ROWS
    projected_per_row = projected_bytes / SAMPLE_ROWS

    with capsys.disabled():
        print(
            f"\n[P1-15] one scan of {SAMPLE_ROWS} rows: "
            f"UsageRecord {full_bytes / _MIB:.2f} MiB ({full_per_row:.0f} B/row) vs "
            f"UsageRollupRow {projected_bytes / _MIB:.2f} MiB ({projected_per_row:.0f} B/row) "
            f"-> {full_bytes / projected_bytes:.2f}x smaller"
        )

    # A projection that saves less than a third of the row is not worth the
    # second parsing path; measured today it saves well over half.
    assert projected_per_row < full_per_row * 0.67


def test_consolidated_overview_holds_far_less_than_the_seven_way_fan_out(capsys):
    """The finding's arithmetic, re-run: 7 full lists vs 1 projected list.

    The seven reports ran concurrently, so their record lists were resident at
    the same time — the fan-out's cost is seven independently-parsed lists, which
    is why this multiplies rather than reusing one list (shared strings would
    flatter it).
    """
    docs = _sample_documents(SAMPLE_ROWS)
    one_full_scan = deep_sizeof([UsageRecord.model_validate(doc) for doc in docs])
    one_projected_scan = deep_sizeof(
        [
            UsageRollupRow.from_document({k: doc[k] for k in ROLLUP_FIELDS if k in doc})
            for doc in docs
        ]
    )

    fan_out = one_full_scan * 7
    consolidated = one_projected_scan
    scale = MAX_ADMIN_RECORDS / SAMPLE_ROWS

    with capsys.disabled():
        print(
            f"[P1-15] at the {MAX_ADMIN_RECORDS:,}-row cap: "
            f"seven-way fan-out ~{fan_out * scale / _MIB:.0f} MiB vs "
            f"consolidated ~{consolidated * scale / _MIB:.0f} MiB "
            f"({fan_out / consolidated:.1f}x less)"
        )

    # The whole point of the finding: a full-cap admin refresh has to fit inside
    # a 1 GiB replica that is also serving chat.
    assert consolidated * scale < 256 * _MIB
    assert consolidated < fan_out * 0.15
