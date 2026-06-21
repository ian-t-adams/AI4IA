"""Org-level (admin) aggregation over the usage ledger.

The per-user :mod:`~ai4ia_api.usage.models` summary answers "what did *I* use".
This module answers the admin's questions — "how many users, tokens, which
models, how many agents" — by aggregating the whole ledger over a bounded window.

Design:
- **Bounded by construction.** Every read goes through ``query_records`` (a
  cross-partition, time-windowed, row-capped scan), so a single dashboard request
  can never trigger an unbounded ledger scan / RU spike. When the cap is hit the
  report carries ``truncated=True`` so the UI can flag an under-count.
- **Pure aggregation.** The math lives in module-level functions over a plain
  ``list[UsageRecord]`` so it is trivially unit-testable against a seeded store;
  :class:`AdminUsageService` only adds the bounded fetch + windowing.
- **Honest counts.** Token totals count only ``usageKnown`` turns and cost totals
  only ``costKnown`` turns (the honesty model), mirroring the per-user
  summary so the two never disagree.
- **Read-only.** Nothing here mutates the ledger.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

from .models import DayUsageBucket, ModelUsageBucket, UsageRecord

# Bound the window so an admin query can never scan unbounded history.
DEFAULT_ADMIN_DAYS = 30
MAX_ADMIN_DAYS = 90
# Hard cap on rows a single admin aggregation request may pull. Generous for a
# personal/demo deployment, but a firm ceiling on RU/memory per request.
MAX_ADMIN_RECORDS = 50_000
# Default page size for the by-user (top spenders) view.
DEFAULT_USER_PAGE_SIZE = 20
MAX_USER_PAGE_SIZE = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AdminUsageWindow(BaseModel):
    """Common window metadata shared by every admin report."""

    sinceDays: int
    fromTime: datetime
    toTime: datetime
    # True when the record-cap was hit, so totals are a (newest-first) lower bound.
    truncated: bool = False
    scannedRecords: int = 0


class AdminUsageSummary(AdminUsageWindow):
    activeUsers: int = 0

    totalRequests: int = 0
    billableRequests: int = 0
    unknownUsageRequests: int = 0
    cancelledRequests: int = 0
    erroredRequests: int = 0
    errorRate: float = 0.0

    totalPromptTokens: int = 0
    totalCompletionTokens: int = 0
    totalTokens: int = 0

    totalCostMicroUsd: int = 0
    costUnknownRequests: int = 0
    currency: str = "USD"

    distinctModels: int = 0
    distinctAgents: int = 0

    @property
    def totalCostUsd(self) -> float:
        return round(self.totalCostMicroUsd / 1_000_000, 6)


class UserUsageBucket(BaseModel):
    userId: str
    requests: int = 0
    erroredRequests: int = 0
    promptTokens: int = 0
    completionTokens: int = 0
    totalTokens: int = 0
    costMicroUsd: int = 0
    costKnown: bool = True
    lastActiveAt: datetime | None = None


class AgentUsageBucket(BaseModel):
    agent: str
    requests: int = 0
    totalTokens: int = 0
    costMicroUsd: int = 0
    users: int = 0  # distinct users who invoked this agent


class AdminByModelReport(AdminUsageWindow):
    byModel: list[ModelUsageBucket] = Field(default_factory=list)


class AdminByDayReport(AdminUsageWindow):
    byDay: list[DayUsageBucket] = Field(default_factory=list)


class AdminByUserReport(AdminUsageWindow):
    totalUsers: int = 0
    limit: int = DEFAULT_USER_PAGE_SIZE
    offset: int = 0
    byUser: list[UserUsageBucket] = Field(default_factory=list)


class AdminAgentsReport(AdminUsageWindow):
    agents: list[AgentUsageBucket] = Field(default_factory=list)


# ---- pure aggregation over a record list ----


def aggregate_summary(records: list[UsageRecord]) -> AdminUsageSummary:
    """Org totals. ``fromTime/toTime/sinceDays`` are filled by the caller."""
    summary = AdminUsageSummary(sinceDays=0, fromTime=_now(), toTime=_now())
    users: set[str] = set()
    models: set[str] = set()
    agents: set[str] = set()
    for rec in records:
        users.add(rec.userId)
        models.add(rec.model)
        if rec.agent:
            agents.add(rec.agent)
        summary.totalRequests += 1
        if rec.status == "cancelled":
            summary.cancelledRequests += 1
        elif rec.status == "error":
            summary.erroredRequests += 1
        if rec.billable:
            summary.billableRequests += 1
        if not rec.usageKnown:
            summary.unknownUsageRequests += 1
        if rec.usageKnown:
            summary.totalPromptTokens += rec.promptTokens or 0
            summary.totalCompletionTokens += rec.completionTokens or 0
            summary.totalTokens += rec.totalTokens or 0
        if rec.costKnown and rec.estCostMicroUsd is not None:
            summary.totalCostMicroUsd += rec.estCostMicroUsd
        elif rec.billable:
            summary.costUnknownRequests += 1
    summary.activeUsers = len(users)
    summary.distinctModels = len(models)
    summary.distinctAgents = len(agents)
    if summary.totalRequests:
        summary.errorRate = round(
            summary.erroredRequests / summary.totalRequests, 4
        )
    return summary


def aggregate_by_model(records: list[UsageRecord]) -> list[ModelUsageBucket]:
    by_model: dict[str, ModelUsageBucket] = {}
    for rec in records:
        bucket = by_model.get(rec.model)
        if bucket is None:
            bucket = ModelUsageBucket(model=rec.model)
            by_model[rec.model] = bucket
        bucket.requests += 1
        if rec.usageKnown:
            bucket.promptTokens += rec.promptTokens or 0
            bucket.completionTokens += rec.completionTokens or 0
            bucket.totalTokens += rec.totalTokens or 0
        if rec.costKnown and rec.estCostMicroUsd is not None:
            bucket.costMicroUsd += rec.estCostMicroUsd
        elif rec.billable:
            bucket.costKnown = False
    return sorted(by_model.values(), key=lambda b: b.totalTokens, reverse=True)


def aggregate_by_day(records: list[UsageRecord]) -> list[DayUsageBucket]:
    by_day: dict[str, DayUsageBucket] = {}
    for rec in records:
        day = rec.createdAt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        bucket = by_day.get(day)
        if bucket is None:
            bucket = DayUsageBucket(day=day)
            by_day[day] = bucket
        bucket.requests += 1
        if rec.usageKnown:
            bucket.totalTokens += rec.totalTokens or 0
        if rec.costKnown and rec.estCostMicroUsd is not None:
            bucket.costMicroUsd += rec.estCostMicroUsd
    return sorted(by_day.values(), key=lambda b: b.day)


def aggregate_by_user(records: list[UsageRecord]) -> list[UserUsageBucket]:
    by_user: dict[str, UserUsageBucket] = {}
    for rec in records:
        bucket = by_user.get(rec.userId)
        if bucket is None:
            bucket = UserUsageBucket(userId=rec.userId)
            by_user[rec.userId] = bucket
        bucket.requests += 1
        if rec.status == "error":
            bucket.erroredRequests += 1
        if rec.usageKnown:
            bucket.promptTokens += rec.promptTokens or 0
            bucket.completionTokens += rec.completionTokens or 0
            bucket.totalTokens += rec.totalTokens or 0
        if rec.costKnown and rec.estCostMicroUsd is not None:
            bucket.costMicroUsd += rec.estCostMicroUsd
        elif rec.billable:
            bucket.costKnown = False
        if bucket.lastActiveAt is None or rec.createdAt > bucket.lastActiveAt:
            bucket.lastActiveAt = rec.createdAt
    # Heaviest consumers first (tokens, then request volume as a tiebreak).
    return sorted(
        by_user.values(), key=lambda b: (b.totalTokens, b.requests), reverse=True
    )


def aggregate_agents(records: list[UsageRecord]) -> list[AgentUsageBucket]:
    by_agent: dict[str, AgentUsageBucket] = {}
    agent_users: dict[str, set[str]] = {}
    for rec in records:
        if not rec.agent:
            continue
        bucket = by_agent.get(rec.agent)
        if bucket is None:
            bucket = AgentUsageBucket(agent=rec.agent)
            by_agent[rec.agent] = bucket
            agent_users[rec.agent] = set()
        bucket.requests += 1
        if rec.usageKnown:
            bucket.totalTokens += rec.totalTokens or 0
        if rec.costKnown and rec.estCostMicroUsd is not None:
            bucket.costMicroUsd += rec.estCostMicroUsd
        agent_users[rec.agent].add(rec.userId)
    for agent, bucket in by_agent.items():
        bucket.users = len(agent_users[agent])
    return sorted(by_agent.values(), key=lambda b: b.totalTokens, reverse=True)


# ---- bounded fetch + windowing ----


def clamp_days(days: int | None) -> int:
    if days is None:
        return DEFAULT_ADMIN_DAYS
    return max(1, min(days, MAX_ADMIN_DAYS))


class AdminUsageService:
    """Reads the ledger cross-partition (bounded) and produces admin reports.

    Shares the same :class:`~ai4ia_api.usage.repository.UsageRepository` instance
    as :class:`~ai4ia_api.usage.service.UsageService`; the metering service owns
    the repo lifecycle (``close``), so this service is read-only and never closes
    it (avoids a double-close of the Cosmos client).
    """

    def __init__(self, repo, *, max_records: int = MAX_ADMIN_RECORDS) -> None:
        self._repo = repo
        self._max_records = max_records

    async def _fetch(self, days: int) -> tuple[list[UsageRecord], int, datetime, datetime, bool]:
        days = clamp_days(days)
        now = _now()
        since = now - timedelta(days=days)
        records = await self._repo.query_records(
            since=since, now=now, limit=self._max_records
        )
        truncated = len(records) >= self._max_records
        return records, days, since, now, truncated

    @staticmethod
    def _meta(days, since, now, records, truncated) -> dict:
        return {
            "sinceDays": days,
            "fromTime": since,
            "toTime": now,
            "truncated": truncated,
            "scannedRecords": len(records),
        }

    async def summary(self, *, days: int | None = None) -> AdminUsageSummary:
        records, days, since, now, truncated = await self._fetch(days or DEFAULT_ADMIN_DAYS)
        summary = aggregate_summary(records)
        meta = self._meta(days, since, now, records, truncated)
        return summary.model_copy(update=meta)

    async def by_model(self, *, days: int | None = None) -> AdminByModelReport:
        records, days, since, now, truncated = await self._fetch(days or DEFAULT_ADMIN_DAYS)
        return AdminByModelReport(
            byModel=aggregate_by_model(records),
            **self._meta(days, since, now, records, truncated),
        )

    async def by_day(self, *, days: int | None = None) -> AdminByDayReport:
        records, days, since, now, truncated = await self._fetch(days or DEFAULT_ADMIN_DAYS)
        return AdminByDayReport(
            byDay=aggregate_by_day(records),
            **self._meta(days, since, now, records, truncated),
        )

    async def by_user(
        self, *, days: int | None = None, limit: int = DEFAULT_USER_PAGE_SIZE, offset: int = 0
    ) -> AdminByUserReport:
        records, days, since, now, truncated = await self._fetch(days or DEFAULT_ADMIN_DAYS)
        ranked = aggregate_by_user(records)
        limit = max(1, min(limit, MAX_USER_PAGE_SIZE))
        offset = max(0, offset)
        page = ranked[offset : offset + limit]
        return AdminByUserReport(
            totalUsers=len(ranked),
            limit=limit,
            offset=offset,
            byUser=page,
            **self._meta(days, since, now, records, truncated),
        )

    async def agents(self, *, days: int | None = None) -> AdminAgentsReport:
        records, days, since, now, truncated = await self._fetch(days or DEFAULT_ADMIN_DAYS)
        return AdminAgentsReport(
            agents=aggregate_agents(records),
            **self._meta(days, since, now, records, truncated),
        )
