"""Admin aggregation math over a seeded ledger (no HTTP).

Asserts the pure aggregation functions and ``AdminUsageService`` produce correct
org-level rollups, honour the usage-honesty model (token totals count only
``usageKnown`` turns; cost totals only ``costKnown`` turns), and stay bounded
(window + record cap -> ``truncated``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ai4ia_api.usage.aggregate import (
    DEFAULT_ADMIN_DAYS,
    MAX_ADMIN_DAYS,
    UNKNOWN_DIMENSION,
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
    aggregate_dimension,
    aggregate_summary,
    aggregate_user_agents,
    clamp_days,
)
from ai4ia_api.usage.memory_repo import InMemoryUsageRepository
from ai4ia_api.usage.models import UsageRecord

NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def rec(
    user: str = "alice",
    model: str = "gpt-5.2",
    *,
    agent: str | None = None,
    status: str = "complete",
    billable: bool = True,
    usage_known: bool = True,
    prompt: int = 10,
    completion: int = 5,
    total: int = 15,
    cost_known: bool = True,
    cost_micro: int | None = 1000,
    created: datetime | None = None,
    provider: str = "azure_openai",
    deployment: str | None = "dep",
    target: str | None = None,
    session: str = "s1",
    region: str | None = None,
    data_zone: str | None = None,
) -> UsageRecord:
    return UsageRecord(
        userId=user,
        sessionId=session,
        provider=provider,
        model=model,
        deployment=deployment,
        target=target,
        agent=agent,
        status=status,
        billable=billable,
        usageKnown=usage_known,
        promptTokens=prompt,
        completionTokens=completion,
        totalTokens=total,
        costKnown=cost_known,
        estCostMicroUsd=cost_micro,
        createdAt=created or NOW,
        region=region,
        dataZone=data_zone,
    )


# ---- clamp_days ----


def test_clamp_days_bounds():
    assert clamp_days(None) == DEFAULT_ADMIN_DAYS
    assert clamp_days(0) == 1
    assert clamp_days(-5) == 1
    assert clamp_days(7) == 7
    assert clamp_days(10_000) == MAX_ADMIN_DAYS


# ---- summary ----


def test_summary_totals_and_distinct_counts():
    records = [
        rec(user="alice", model="gpt-5.2", agent="research", total=15, cost_micro=1000),
        rec(user="bob", model="gpt-5.2", agent="research", total=30, cost_micro=2000),
        rec(user="bob", model="o3", agent=None, total=10, cost_micro=500),
    ]
    s = aggregate_summary(records)
    assert s.activeUsers == 2
    assert s.totalRequests == 3
    assert s.totalPromptTokens == 30  # 10 * 3
    assert s.totalCompletionTokens == 15  # 5 * 3
    assert s.totalTokens == 55
    assert s.totalCostMicroUsd == 3500
    assert s.totalCostUsd == 0.0035
    assert s.distinctModels == 2
    assert s.distinctAgents == 1
    assert s.distinctProviders == 1
    assert s.errorRate == 0.0


def test_summary_honesty_unknown_usage_and_cost():
    records = [
        # Known + priced.
        rec(total=15, cost_micro=1000, usage_known=True, cost_known=True),
        # Usage NOT known: tokens must not be summed, counts as unknownUsage.
        rec(total=999, cost_known=False, cost_micro=None, usage_known=False, billable=True),
        # Billable but cost unknown: counted as costUnknown, not added to total.
        rec(total=20, cost_known=False, cost_micro=None, usage_known=True, billable=True),
    ]
    s = aggregate_summary(records)
    assert s.totalTokens == 35  # 15 + 20 (the unknown-usage 999 excluded)
    assert s.unknownUsageRequests == 1
    assert s.totalCostMicroUsd == 1000  # only the priced turn
    assert s.costUnknownRequests == 2  # two billable turns with no known cost


def test_summary_counts_distinct_providers():
    records = [
        rec(provider="azure_openai"),
        rec(provider="speech_voice_live", deployment=None, target="managed_voice_live"),
    ]
    s = aggregate_summary(records)
    assert s.distinctProviders == 2


def test_summary_error_rate():
    records = [
        rec(status="complete"),
        rec(status="error", billable=False),
        rec(status="error", billable=False),
        rec(status="cancelled", billable=False),
    ]
    s = aggregate_summary(records)
    assert s.erroredRequests == 2
    assert s.cancelledRequests == 1
    assert s.errorRate == 0.5  # 2 / 4


def test_summary_empty():
    s = aggregate_summary([])
    assert s.totalRequests == 0
    assert s.activeUsers == 0
    assert s.errorRate == 0.0


# ---- by-model ----


def test_by_model_grouped_and_sorted_desc():
    records = [
        rec(model="small", total=5, cost_micro=100),
        rec(model="big", total=100, cost_micro=5000),
        rec(model="big", total=50, cost_micro=2500),
    ]
    out = aggregate_by_model(records)
    assert [b.model for b in out] == ["big", "small"]  # tokens desc
    big = out[0]
    assert big.requests == 2
    assert big.totalTokens == 150
    assert big.costMicroUsd == 7500
    assert big.costKnown is True


def test_by_model_cost_unknown_marks_bucket():
    records = [
        rec(model="m", total=10, cost_known=False, cost_micro=None, billable=True),
    ]
    out = aggregate_by_model(records)
    assert out[0].costKnown is False


# ---- by-day ----


def test_by_day_groups_utc_and_sorted_asc():
    d1 = datetime(2024, 6, 13, 23, 0, tzinfo=timezone.utc)
    d2 = datetime(2024, 6, 14, 1, 0, tzinfo=timezone.utc)
    d2b = datetime(2024, 6, 14, 5, 0, tzinfo=timezone.utc)
    records = [
        rec(created=d2, total=10, cost_micro=100),
        rec(created=d1, total=5, cost_micro=50),
        rec(created=d2b, total=20, cost_micro=200),
    ]
    out = aggregate_by_day(records)
    assert [b.day for b in out] == ["2024-06-13", "2024-06-14"]
    assert out[1].requests == 2
    assert out[1].totalTokens == 30
    assert out[1].costMicroUsd == 300


# ---- by-user ----


def test_by_user_ranked_by_tokens_then_requests():
    records = [
        rec(user="light", total=10),
        rec(user="heavy", total=100),
        rec(user="heavy", total=100),
        rec(user="mid", total=50),
    ]
    out = aggregate_by_user(records)
    assert [b.userId for b in out] == ["heavy", "mid", "light"]
    heavy = out[0]
    assert heavy.requests == 2
    assert heavy.totalTokens == 200


def test_by_user_tracks_errors_and_last_active():
    early = NOW - timedelta(hours=2)
    late = NOW
    records = [
        rec(user="u", created=early, status="complete", total=10),
        rec(user="u", created=late, status="error", billable=False, total=0),
    ]
    out = aggregate_by_user(records)
    bucket = out[0]
    assert bucket.erroredRequests == 1
    assert bucket.lastActiveAt == late


# ---- agents ----


def test_agents_distinct_users_and_tokens():
    records = [
        rec(user="a", agent="research", total=10),
        rec(user="b", agent="research", total=20),
        rec(user="a", agent="research", total=5),
        rec(user="a", agent="coder", total=100),
        rec(user="a", agent=None, total=999),  # no agent -> excluded
    ]
    out = aggregate_agents(records)
    assert [b.agent for b in out] == ["coder", "research"]  # tokens desc (100 vs 35)
    research = next(b for b in out if b.agent == "research")
    assert research.requests == 3
    assert research.totalTokens == 35
    assert research.users == 2  # a and b


def test_agents_count_errored_and_cancelled():
    records = [
        rec(user="a", agent="research", status="complete", total=10),
        rec(user="a", agent="research", status="error", billable=False, total=0),
        rec(user="b", agent="research", status="cancelled", billable=False, total=0),
        rec(user="b", agent="research", status="error", billable=False, total=0),
        rec(user="a", agent="coder", status="complete", total=5),
    ]
    out = aggregate_agents(records)
    research = next(b for b in out if b.agent == "research")
    assert research.requests == 4
    assert research.erroredRequests == 2
    assert research.cancelledRequests == 1
    coder = next(b for b in out if b.agent == "coder")
    assert coder.erroredRequests == 0
    assert coder.cancelledRequests == 0


# ---- user×agent cross-tab ----


def test_user_agents_cross_tab_groups_and_skips_plain_turns():
    records = [
        rec(user="alice", agent="research", total=10),
        rec(user="alice", agent="research", total=20),
        rec(user="alice", agent="coder", total=5),
        rec(user="bob", agent="research", total=100),
        rec(user="bob", agent=None, total=999),  # plain turn -> excluded
    ]
    out = aggregate_user_agents(records)
    # One cell per (user, agent); plain turn dropped -> 3 cells.
    cells = {(b.userId, b.agent): b for b in out}
    assert set(cells) == {("alice", "research"), ("alice", "coder"), ("bob", "research")}
    alice_research = cells[("alice", "research")]
    assert alice_research.requests == 2
    assert alice_research.totalTokens == 30
    # Heaviest cell (bob/research, 100 tokens) ranks first.
    assert (out[0].userId, out[0].agent) == ("bob", "research")


def test_user_agents_counts_errors_per_cell():
    records = [
        rec(user="alice", agent="research", status="complete", total=10),
        rec(user="alice", agent="research", status="error", billable=False, total=0),
    ]
    out = aggregate_user_agents(records)
    assert len(out) == 1
    assert out[0].requests == 2
    assert out[0].erroredRequests == 1
    assert out[0].totalTokens == 10


# ---- dimension rollups (region / dataZone / deployment / status) ----


def test_dimension_rollup_groups_counts_and_unknown_key():
    records = [
        rec(region="eastus", total=10),
        rec(region="eastus", total=20, status="error", billable=False),
        rec(region="westus", total=5),
        rec(region=None, total=7),  # missing -> UNKNOWN_DIMENSION
    ]
    out = aggregate_by_region(records)
    by_key = {b.key: b for b in out}
    assert by_key["eastus"].requests == 2
    assert by_key["eastus"].erroredRequests == 1
    assert by_key["eastus"].totalTokens == 30
    assert by_key["westus"].requests == 1
    assert UNKNOWN_DIMENSION in by_key
    # Sorted by request volume desc -> eastus (2) first.
    assert out[0].key == "eastus"


def test_dimension_rollup_honours_cost_honesty():
    records = [
        rec(deployment="d", cost_known=False, cost_micro=None, billable=True, total=10),
    ]
    out = aggregate_by_deployment(records)
    assert out[0].key == "d"
    assert out[0].costKnown is False


def test_dimension_rollup_groups_null_deployment_as_unknown():
    records = [
        rec(
            provider="speech_voice_live",
            deployment=None,
            target="managed_voice_live",
            total=10,
            cost_known=False,
            cost_micro=None,
            billable=False,
        ),
    ]
    out = aggregate_by_deployment(records)
    assert out[0].key == UNKNOWN_DIMENSION


def test_by_provider_rollup_groups_by_provider():
    records = [
        rec(provider="azure_openai", total=10),
        rec(provider="speech_voice_live", deployment=None, target="managed_voice_live", total=20),
    ]
    out = aggregate_by_provider(records)
    by_key = {b.key: b for b in out}
    assert by_key["azure_openai"].requests == 1
    assert by_key["speech_voice_live"].requests == 1


def test_aggregate_by_data_zone_and_status_mix():
    records = [
        rec(data_zone="us", status="complete", total=10),
        rec(data_zone="us", status="error", billable=False, total=0),
        rec(data_zone="eu", status="cancelled", billable=False, total=0),
    ]
    zones = {b.key: b for b in aggregate_by_data_zone(records)}
    assert zones["us"].requests == 2
    assert zones["eu"].requests == 1

    statuses = {b.key: b for b in aggregate_by_status(records)}
    assert statuses["complete"].requests == 1
    assert statuses["error"].requests == 1
    assert statuses["cancelled"].requests == 1
    # The error-status bucket's erroredRequests mirrors its request count.
    assert statuses["error"].erroredRequests == 1


def test_aggregate_dimension_generic_custom_key():
    records = [rec(model="a", total=1), rec(model="a", total=1), rec(model="b", total=1)]
    out = aggregate_dimension(records, lambda r: r.model)
    assert {b.key: b.requests for b in out} == {"a": 2, "b": 1}


# ---- AdminUsageService (bounded fetch + windowing) ----


async def _service_with(records: list[UsageRecord], **kw) -> AdminUsageService:
    repo = InMemoryUsageRepository()
    for r in records:
        await repo.record(r)
    return AdminUsageService(repo, **kw)


async def test_service_summary_window_excludes_old_records():
    fresh = rec(user="recent", created=datetime.now(timezone.utc), total=10)
    stale = rec(
        user="old",
        created=datetime.now(timezone.utc) - timedelta(days=120),
        total=999,
    )
    svc = await _service_with([fresh, stale])
    s = await svc.summary(days=30)
    assert s.totalRequests == 1
    assert s.totalTokens == 10
    assert s.sinceDays == 30
    assert s.scannedRecords == 1


async def test_service_truncation_flag_when_cap_hit():
    now = datetime.now(timezone.utc)
    records = [rec(user=f"u{i}", created=now, total=1) for i in range(5)]
    svc = await _service_with(records, max_records=3)
    s = await svc.summary(days=30)
    assert s.scannedRecords == 3
    assert s.truncated is True


async def test_service_by_user_paging():
    now = datetime.now(timezone.utc)
    records = [rec(user=f"u{i}", created=now, total=(10 - i)) for i in range(5)]
    svc = await _service_with(records)
    page = await svc.by_user(days=30, limit=2, offset=0)
    assert page.totalUsers == 5
    assert page.limit == 2
    assert len(page.byUser) == 2
    assert page.byUser[0].userId == "u0"  # highest tokens first
    page2 = await svc.by_user(days=30, limit=2, offset=2)
    assert [b.userId for b in page2.byUser] == ["u2", "u3"]


async def test_service_user_agents_windowed():
    now = datetime.now(timezone.utc)
    fresh = rec(user="alice", agent="research", created=now, total=10)
    stale = rec(
        user="bob",
        agent="research",
        created=now - timedelta(days=120),
        total=999,
    )
    svc = await _service_with([fresh, stale])
    report = await svc.user_agents(days=30)
    assert report.sinceDays == 30
    assert report.scannedRecords == 1
    assert [(c.userId, c.agent) for c in report.userAgents] == [("alice", "research")]


async def test_service_distributions_windowed_rollups():
    now = datetime.now(timezone.utc)
    records = [
        rec(user="a", region="eastus", data_zone="us", deployment="d1", status="complete", created=now, total=10),
        rec(user="b", region="eastus", data_zone="us", deployment="d2", status="error", billable=False, created=now, total=0),
        rec(
            user="c",
            region="westus",
            data_zone="eu",
            provider="speech_voice_live",
            deployment=None,
            target="managed_voice_live",
            status="cancelled",
            billable=False,
            created=now,
            total=0,
        ),
        rec(user="old", region="eastus", created=now - timedelta(days=120), total=999),
    ]
    svc = await _service_with(records)
    report = await svc.distributions(days=30)
    assert report.scannedRecords == 3  # stale record excluded
    regions = {b.key: b.requests for b in report.byRegion}
    assert regions == {"eastus": 2, "westus": 1}
    zones = {b.key: b.requests for b in report.byDataZone}
    assert zones == {"us": 2, "eu": 1}
    providers = {b.key: b.requests for b in report.byProvider}
    assert providers == {"azure_openai": 2, "speech_voice_live": 1}
    deployments = {b.key: b.requests for b in report.byDeployment}
    assert deployments == {"d1": 1, "d2": 1, UNKNOWN_DIMENSION: 1}
    statuses = {b.key: b.requests for b in report.byStatus}
    assert statuses == {"complete": 1, "error": 1, "cancelled": 1}
