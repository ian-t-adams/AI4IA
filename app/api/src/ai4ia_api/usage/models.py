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

from collections.abc import Mapping
from dataclasses import dataclass
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..catalog import DeploymentOption

UsageStatus = Literal["complete", "cancelled", "error"]

#: Provider identity for a direct Code Interpreter sandbox execution. Deliberately
#: distinct from ``azure_openai`` so admin rollups (``aggregate_by_provider``) can
#: separate sandbox spend from chat spend rather than letting it hide inside the
#: parent chat charge — and so the rolling ``computeExecutionsPerDay`` window has
#: an unambiguous predicate to count. Code Interpreter is the documented
#: direct-to-Foundry exception (a stateful Azure-managed sandbox is not a routable
#: chat-completions deployment), which is exactly why it needs its own identity.
CODE_INTERPRETER_PROVIDER = "azure_openai_code_interpreter"


def cost_bearing_attempt(rec: "UsageRollupSource") -> bool:
    """Whether a row represents provider work whose unknown cost matters.

    Token-priced chat rows historically use ``billable`` for this. Direct Code
    Interpreter rows cannot: the provider reports no token usage, so they are
    intentionally ``usageKnown=False`` and therefore ``billable=False`` even
    though each recorded row is a sandbox execution attempt that may incur cost.
    Treating billability as the cost signal made every sandbox rollup report
    known ``$0`` with zero unknown-cost requests.
    """

    return rec.billable or rec.provider == CODE_INTERPRETER_PROVIDER
#: Target/agent label carried alongside the provider, so the admin agents panel
#: also shows sandbox executions as their own row.
CODE_INTERPRETER_TARGET = "code_interpreter"
#: Synthetic model id for the ledger row when no CI deployment name is configured.
CODE_INTERPRETER_MODEL = "code-interpreter"


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


@dataclass(frozen=True, slots=True)
class UsageTarget:
    """Typed descriptor for a governed usage target."""

    provider: str = "azure_openai"
    deployment: str | None = None
    target: str | None = None
    region: str | None = None
    dataZone: str | None = None

    @classmethod
    def from_deployment(
        cls, deployment: DeploymentOption, *, provider: str = "azure_openai"
    ) -> "UsageTarget":
        return cls(
            provider=provider,
            deployment=deployment.deploymentName,
            target=deployment.deploymentName,
            region=deployment.region,
            dataZone=deployment.dataZone,
        )

    @classmethod
    def managed_service(
        cls,
        *,
        provider: str,
        target: str,
        region: str,
        data_zone: str | None = None,
    ) -> "UsageTarget":
        return cls(
            provider=provider,
            deployment=None,
            target=target,
            region=region,
            dataZone=data_zone,
        )

    @classmethod
    def code_interpreter(cls, deployment: str | None = None) -> "UsageTarget":
        """The distinct identity every direct Code Interpreter execution is
        metered under.

        ``deployment`` is the configured CI deployment name when there is one, so
        an operator can still tell *which* deployment served the sandbox; the
        provider/target pair is what keeps the spend out of the chat bucket.
        Region is unknown from the app's side (the sandbox container is
        Azure-managed and its placement is not reported), and saying so is more
        honest than inventing one.
        """
        return cls(
            provider=CODE_INTERPRETER_PROVIDER,
            deployment=deployment or None,
            target=CODE_INTERPRETER_TARGET,
            region=None,
            dataZone=None,
        )


class UsageRecord(BaseModel):
    """One metered chat turn in the per-user ledger (Cosmos PK ``/userId``)."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    userId: str
    sessionId: str
    provider: str = "azure_openai"
    model: str
    deployment: str | None = None
    target: str | None = None
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


@runtime_checkable
class UsageRollupSource(Protocol):
    """Exactly the ledger fields the admin rollups read — nothing else.

    This is the *contract* between :mod:`ai4ia_api.usage.aggregate` and whatever
    is handed to it. :class:`UsageRecord` (the full ledger row) satisfies it, and
    so does :class:`UsageRollupRow` (the projected row an admin scan actually
    fetches), so the aggregation math is written once and runs unchanged over
    either. Members are read-only properties on purpose: the aggregates never
    mutate a row, and covariance lets ``status`` accept the narrower
    :data:`UsageStatus` literal that ``UsageRecord`` declares.

    Adding a field to an aggregate means adding it here *and* to
    :class:`UsageRollupRow` and the projection in the Cosmos repo — otherwise the
    projected path would silently read a default instead of the stored value.
    """

    @property
    def userId(self) -> str: ...
    @property
    def model(self) -> str: ...
    @property
    def provider(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def billable(self) -> bool: ...
    @property
    def usageKnown(self) -> bool: ...
    @property
    def costKnown(self) -> bool: ...
    @property
    def createdAt(self) -> datetime: ...
    @property
    def agent(self) -> str | None: ...
    @property
    def deployment(self) -> str | None: ...
    @property
    def region(self) -> str | None: ...
    @property
    def dataZone(self) -> str | None: ...
    @property
    def promptTokens(self) -> int | None: ...
    @property
    def completionTokens(self) -> int | None: ...
    @property
    def totalTokens(self) -> int | None: ...
    @property
    def estCostMicroUsd(self) -> int | None: ...


def _coerce_datetime(value: Any) -> datetime:
    """Parse a ledger ``createdAt`` the same way pydantic would, but cheaply.

    Rows are always written through ``model_dump(mode="json")``, so the stored
    value is an ISO-8601 string (``…Z`` for UTC). A naive value is treated as UTC,
    matching how the rest of the ledger is written. An unparseable value raises,
    so a corrupt row fails loudly rather than silently landing in the wrong day
    bucket — the same posture as ``UsageRecord.model_validate`` today.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise ValueError(f"unparseable ledger createdAt: {type(value).__name__}")


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


@dataclass(frozen=True, slots=True)
class UsageRollupRow:
    """A ledger row projected down to the fields :mod:`.aggregate` reads.

    Why this exists: the admin dashboard scans up to ``MAX_ADMIN_RECORDS`` rows
    per request, and a full :class:`UsageRecord` carries ten fields no rollup
    touches (``id``, ``sessionId``, ``target``, ``usageComplete``, ``calls``,
    ``currency``, the three price snapshot fields, ``correlationId``) plus
    pydantic's per-instance ``__dict__`` and ``__pydantic_fields_set__``. A frozen
    slotted dataclass drops both, which is what keeps a 50,000-row admin window
    inside a 1 GiB replica (see tests/test_admin_usage_overview.py for the
    measured figures).

    Accuracy is not traded away for that: :func:`from_record` and
    :func:`from_document` are asserted to produce byte-identical rollups to the
    full-record path, and the ``usageKnown``/``costKnown`` honesty flags are
    carried verbatim so unknown never collapses into zero.
    """

    userId: str
    model: str
    provider: str
    status: str
    billable: bool
    usageKnown: bool
    costKnown: bool
    createdAt: datetime
    agent: str | None = None
    deployment: str | None = None
    region: str | None = None
    dataZone: str | None = None
    promptTokens: int | None = None
    completionTokens: int | None = None
    totalTokens: int | None = None
    estCostMicroUsd: int | None = None

    @classmethod
    def from_record(cls, record: UsageRollupSource) -> "UsageRollupRow":
        """Project an already-materialized row (used by the in-memory ledger)."""
        return cls(
            userId=record.userId,
            model=record.model,
            provider=record.provider,
            status=record.status,
            billable=record.billable,
            usageKnown=record.usageKnown,
            costKnown=record.costKnown,
            createdAt=record.createdAt,
            agent=record.agent,
            deployment=record.deployment,
            region=record.region,
            dataZone=record.dataZone,
            promptTokens=record.promptTokens,
            completionTokens=record.completionTokens,
            totalTokens=record.totalTokens,
            estCostMicroUsd=record.estCostMicroUsd,
        )

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> "UsageRollupRow":
        """Build from a projected Cosmos document.

        A projection omits keys the stored document did not have, so every
        optional field defaults exactly as :class:`UsageRecord` would. ``provider``
        keeps the same historical default (rows written before ``provider``
        existed are Azure OpenAI turns).
        """
        return cls(
            userId=str(doc.get("userId") or ""),
            model=str(doc.get("model") or ""),
            provider=str(doc.get("provider") or "azure_openai"),
            status=str(doc.get("status") or "complete"),
            billable=bool(doc.get("billable", False)),
            usageKnown=bool(doc.get("usageKnown", False)),
            costKnown=bool(doc.get("costKnown", False)),
            createdAt=_coerce_datetime(doc.get("createdAt")),
            agent=_coerce_optional_str(doc.get("agent")),
            deployment=_coerce_optional_str(doc.get("deployment")),
            region=_coerce_optional_str(doc.get("region")),
            dataZone=_coerce_optional_str(doc.get("dataZone")),
            promptTokens=_coerce_int(doc.get("promptTokens")),
            completionTokens=_coerce_int(doc.get("completionTokens")),
            totalTokens=_coerce_int(doc.get("totalTokens")),
            estCostMicroUsd=_coerce_int(doc.get("estCostMicroUsd")),
        )


#: Ledger fields a rollup scan projects. Ordered for a stable, reviewable
#: ``SELECT`` list; kept beside the row it fills so the two cannot drift.
ROLLUP_FIELDS: tuple[str, ...] = (
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
)


class WindowTotals(BaseModel):
    """Lightweight rolling-window aggregate used by entitlement enforcement.

    A tiny projection of :class:`UsageSummary` over an arbitrary ``[since, now]``
    window (the summary type carries far more than enforcement needs). Kept here
    so the ``usage`` package stays the single owner of every ledger read.
    """

    requests: int = 0
    totalTokens: int = 0
    costMicroUsd: int = 0
    #: Rows metered under :data:`CODE_INTERPRETER_PROVIDER` — i.e. direct sandbox
    #: executions. Counts *attempts* (errored executions are recorded too), which
    #: is the point: a sandbox that spun up and then failed still cost money.
    computeExecutions: int = 0


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
    # Direct Code Interpreter sandbox executions in this window (a subset of
    # totalRequests). Its own counter because a sandbox is billed per session
    # rather than per token, so it is invisible in the token/cost totals — every
    # such row is deliberately usage-unknown, never zero.
    computeExecutions: int = 0

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


class SessionUsageSummary(BaseModel):
    sessionId: str
    totalRequests: int = 0
    totalPromptTokens: int = 0
    totalCompletionTokens: int = 0
    totalTokens: int = 0
    totalCostMicroUsd: int = 0
    unknownUsageRequests: int = 0
    costUnknownRequests: int = 0
    latest: UsageRecord | None = None
    truncated: bool = False
    coveredRequests: int = 0
    coverageStart: datetime | None = None
    coverageEnd: datetime | None = None


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
        if rec.provider == CODE_INTERPRETER_PROVIDER:
            summary.computeExecutions += 1
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
        elif cost_bearing_attempt(rec):
            summary.costUnknownRequests += 1

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
        elif cost_bearing_attempt(rec):
            bucket.costKnown = False

        day = rec.createdAt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        dbucket = by_day.get(day)
        if dbucket is None:
            dbucket = DayUsageBucket(day=day)
            by_day[day] = dbucket
        dbucket.requests += 1
        if rec.usageKnown:
            dbucket.totalTokens += rec.totalTokens or 0
        if rec.costKnown and rec.estCostMicroUsd is not None:
            dbucket.costMicroUsd += rec.estCostMicroUsd

    summary.byModel = sorted(
        by_model.values(), key=lambda b: b.totalTokens, reverse=True
    )
    summary.byDay = sorted(by_day.values(), key=lambda b: b.day)
    return summary
