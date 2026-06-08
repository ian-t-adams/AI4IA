"""Usage data model: token aggregation + the persisted ledger record/summary.

``TokenUsage`` is the in-process aggregation type produced by the gateway/agent
layers and consumed by the metering service. It carries explicit *known* and
*complete* flags so unreported usage is never confused with zero usage:

- ``known``    : at least one real ``usage`` object was observed.
- ``complete`` : EVERY underlying model call reported usage (an agent turn makes
                 several calls; if any one omits usage the aggregate is partial).

``UsageRecord`` is the durable, per-user ledger row. ``UsageSummary`` is the
aggregate shape returned by ``GET /api/usage``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

UsageStatus = Literal["complete", "cancelled", "error"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TokenUsage(BaseModel):
    """Aggregated token counts with explicit completeness semantics."""

    prompt: int = 0
    completion: int = 0
    total: int = 0
    # Whether any real usage was observed at all.
    known: bool = False
    # Whether usage was observed for *every* contributing model call.
    complete: bool = True
    # Number of model calls that contributed (1 for a plain turn, N for agents).
    calls: int = 0

    @classmethod
    def empty(cls) -> "TokenUsage":
        return cls()

    @classmethod
    def parse(cls, usage: dict[str, Any] | None) -> "TokenUsage":
        """Parse one model response ``usage`` object into a single-call total.

        A missing/empty/malformed object yields an *unknown* single call
        (``known=False``, ``complete=False``) so it is never summed as zero.
        Fully defensive: malformed numeric fields never raise, so metering can
        never break the chat turn that produced them.
        """
        if not usage or not isinstance(usage, dict):
            return cls(known=False, complete=False, calls=1)
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        total = usage.get("total_tokens")
        if prompt is None and completion is None and total is None:
            return cls(known=False, complete=False, calls=1)
        try:
            p = int(prompt or 0)
            c = int(completion or 0)
            t = int(total if total is not None else p + c)
        except (TypeError, ValueError, OverflowError):
            # Provider returned non-numeric usage: treat as unknown, never zero.
            return cls(known=False, complete=False, calls=1)
        return cls(prompt=p, completion=c, total=t, known=True, complete=True, calls=1)

    def add(self, other: "TokenUsage") -> "TokenUsage":
        """Fold another call's usage in (used to aggregate an agent turn).

        ``complete`` stays true only while every folded call was itself known;
        ``known`` becomes true if any folded call was known.
        """
        return TokenUsage(
            prompt=self.prompt + other.prompt,
            completion=self.completion + other.completion,
            total=self.total + other.total,
            known=self.known or other.known,
            complete=self.complete and other.complete and (other.calls == 0 or other.known),
            calls=self.calls + other.calls,
        )


class UsageRecord(BaseModel):
    """One metered chat turn in the per-user ledger (Cosmos PK ``/userId``)."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    userId: str
    sessionId: str
    model: str
    deployment: str
    region: str | None = None
    dataZone: str | None = None
    agent: str | None = None

    status: UsageStatus = "complete"
    # A turn is billable only when it completed and reported real usage.
    billable: bool = False
    usageKnown: bool = False
    # False when an agent turn had at least one call without reported usage.
    usageComplete: bool = True
    calls: int = 0

    promptTokens: int | None = None
    completionTokens: int | None = None
    totalTokens: int | None = None

    # Cost stored as integer micro-USD to avoid float drift in accumulated
    # totals; ``estCostUsd`` is a display convenience derived from it.
    costKnown: bool = False
    estCostMicroUsd: int | None = None
    currency: str = "USD"
    # Snapshot of the rates used so summaries never recompute from current prices.
    priceInputPer1M: float | None = None
    priceOutputPer1M: float | None = None
    priceVersion: str | None = None

    correlationId: str | None = None
    createdAt: datetime = Field(default_factory=_now)

    @property
    def estCostUsd(self) -> float | None:
        if self.estCostMicroUsd is None:
            return None
        return round(self.estCostMicroUsd / 1_000_000, 6)


class ModelUsageBucket(BaseModel):
    model: str
    requests: int = 0
    promptTokens: int = 0
    completionTokens: int = 0
    totalTokens: int = 0
    costMicroUsd: int = 0
    costKnown: bool = True


class DayUsageBucket(BaseModel):
    day: str  # YYYY-MM-DD (UTC)
    requests: int = 0
    totalTokens: int = 0
    costMicroUsd: int = 0


class UsageSummary(BaseModel):
    userId: str
    sinceDays: int
    fromTime: datetime
    toTime: datetime

    totalRequests: int = 0
    billableRequests: int = 0
    # Requests where the upstream did not report token usage.
    unknownUsageRequests: int = 0
    cancelledRequests: int = 0
    erroredRequests: int = 0

    totalPromptTokens: int = 0
    totalCompletionTokens: int = 0
    totalTokens: int = 0

    # Sum of known costs only. ``costUnknownRequests`` counts metered turns whose
    # cost could not be estimated (unknown usage or no price), so the total is
    # never silently understated without a signal.
    totalCostMicroUsd: int = 0
    costUnknownRequests: int = 0
    currency: str = "USD"

    byModel: list[ModelUsageBucket] = Field(default_factory=list)
    byDay: list[DayUsageBucket] = Field(default_factory=list)

    @property
    def totalCostUsd(self) -> float:
        return round(self.totalCostMicroUsd / 1_000_000, 6)


def summarize_records(
    user_id: str,
    records: list[UsageRecord],
    *,
    since_days: int,
    from_time: datetime,
    to_time: datetime,
) -> UsageSummary:
    """Aggregate raw ledger rows into a summary (shared by all repo impls)."""
    summary = UsageSummary(
        userId=user_id,
        sinceDays=since_days,
        fromTime=from_time,
        toTime=to_time,
    )
    by_model: dict[str, ModelUsageBucket] = {}
    by_day: dict[str, DayUsageBucket] = {}

    for rec in records:
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

        bucket = by_model.get(rec.model)
        if bucket is None:
            bucket = ModelUsageBucket(model=rec.model)
            by_model[rec.model] = bucket
        bucket.requests += 1
        bucket.promptTokens += rec.promptTokens or 0
        bucket.completionTokens += rec.completionTokens or 0
        bucket.totalTokens += rec.totalTokens or 0
        if rec.costKnown and rec.estCostMicroUsd is not None:
            bucket.costMicroUsd += rec.estCostMicroUsd
        elif rec.billable:
            bucket.costKnown = False

        day = rec.createdAt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        dbucket = by_day.get(day)
        if dbucket is None:
            dbucket = DayUsageBucket(day=day)
            by_day[day] = dbucket
        dbucket.requests += 1
        dbucket.totalTokens += rec.totalTokens or 0
        if rec.costKnown and rec.estCostMicroUsd is not None:
            dbucket.costMicroUsd += rec.estCostMicroUsd

    summary.byModel = sorted(
        by_model.values(), key=lambda b: b.totalTokens, reverse=True
    )
    summary.byDay = sorted(by_day.values(), key=lambda b: b.day)
    return summary
