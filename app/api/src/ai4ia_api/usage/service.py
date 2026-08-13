"""Usage metering service: build a ledger record, estimate cost, persist + log.

This sits at the chat layer (which knows the user, model, deployment and agent)
and is the single place that turns a completed turn's :class:`TokenUsage` into a
durable :class:`UsageRecord`. Persistence is best-effort and self-contained: a
failing ledger write or price lookup never propagates to the chat response.

Cost honesty rules (see ``usage`` package docstring):
- A turn is ``billable`` only when provider work completed AND usage was both known
  and complete (every contributing model call reported usage). Downstream delivery
  can still end in an error/cancellation after billable provider work.
- Cost is estimated only for billable turns with a known price; otherwise the
  record is ``costKnown == False`` and the summary counts it as cost-unknown.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime

from ..catalog import DeploymentOption
from ..chat_timing import ChatTiming
from ..logging_setup import emit_custom_event
from .models import (
    TokenUsage,
    UsageRecord,
    cost_bearing_attempt,
    UsageStatus,
    UsageSummary,
    SessionUsageSummary,
    UsageTarget,
    WindowTotals,
)
from .pricing import PricingBook
from .repository import UsageRepository

logger = logging.getLogger(__name__)

# Bound the summary window so a query can never scan an unbounded history.
DEFAULT_SUMMARY_DAYS = 30
MAX_SUMMARY_DAYS = 90


def _telemetry_identifier(kind: str, value: str) -> str:
    """Stable one-way identifier for logs/events; never emit ledger keys raw."""
    material = f"ai4ia:{kind}:{value}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


class UsageService:
    def __init__(
        self,
        repo: UsageRepository,
        pricing: PricingBook,
        *,
        enabled: bool = True,
    ) -> None:
        self._repo = repo
        self._pricing = pricing
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _normalize_target(
        target: UsageTarget | DeploymentOption | None,
        deployment: DeploymentOption | None,
    ) -> UsageTarget:
        if target is not None:
            if isinstance(target, DeploymentOption):
                return UsageTarget.from_deployment(target)
            return target
        if deployment is not None:
            return UsageTarget.from_deployment(deployment)
        raise ValueError("A usage target or deployment is required.")

    def build_record(
        self,
        *,
        user_id: str,
        session_id: str,
        model_id: str,
        target: UsageTarget | DeploymentOption | None = None,
        deployment: DeploymentOption | None = None,
        usage: TokenUsage,
        status: UsageStatus,
        agent: str | None,
        correlation_id: str | None,
        provider_completed: bool | None = None,
        billable_units: int | None = None,
        billing_unit: str | None = None,
        image_size: str | None = None,
        image_quality: str | None = None,
    ) -> UsageRecord:
        descriptor = self._normalize_target(target, deployment)
        completed = status == "complete" if provider_completed is None else provider_completed
        unit_billable = (
            completed
            and billing_unit in {"image", "page"}
            and billable_units is not None
            and billable_units > 0
        )
        unit_count = billable_units if unit_billable else None
        billable = unit_billable or (completed and usage.known and usage.complete)
        rec = UsageRecord(
            userId=user_id,
            sessionId=session_id,
            provider=descriptor.provider,
            model=model_id,
            deployment=descriptor.deployment,
            target=descriptor.target,
            region=descriptor.region,
            dataZone=descriptor.dataZone,
            agent=agent,
            status=status,
            providerCompleted=completed,
            billable=billable,
            usageKnown=usage.known,
            usageComplete=usage.complete,
            calls=usage.calls,
            promptTokens=usage.prompt if usage.known else None,
            completionTokens=usage.completion if usage.known else None,
            totalTokens=usage.total if usage.known else None,
            billableUnits=float(unit_count) if unit_count is not None else None,
            billingUnit=billing_unit if unit_billable else None,
            imageSize=image_size,
            imageQuality=image_quality,
            correlationId=correlation_id,
        )
        if unit_count is not None and billing_unit == "image":
            operation_est = self._pricing.estimate_image(
                model_id,
                size=image_size,
                quality=image_quality,
                count=unit_count,
            )
            rec.currency = operation_est.currency
            rec.priceVersion = operation_est.version
            rec.pricingBasis = operation_est.pricing_basis
            if operation_est.known and operation_est.micro_usd is not None:
                rec.costKnown = True
                rec.estCostMicroUsd = operation_est.micro_usd
        elif unit_count is not None and billing_unit == "page":
            operation_est = self._pricing.estimate_pages(
                model_id, pages=unit_count
            )
            rec.currency = operation_est.currency
            rec.priceVersion = operation_est.version
            rec.pricingBasis = operation_est.pricing_basis
            if operation_est.known and operation_est.micro_usd is not None:
                rec.costKnown = True
                rec.estCostMicroUsd = operation_est.micro_usd
        elif billable:
            est = self._pricing.estimate(
                model_id,
                prompt_tokens=usage.prompt,
                completion_tokens=usage.completion,
            )
            rec.currency = est.currency
            rec.priceVersion = est.version
            rec.priceInputPer1M = est.input_per_1m
            rec.priceOutputPer1M = est.output_per_1m
            if est.known and est.micro_usd is not None:
                rec.costKnown = True
                rec.estCostMicroUsd = est.micro_usd
        return rec

    async def record_completion(
        self,
        *,
        user_id: str,
        session_id: str,
        model_id: str,
        target: UsageTarget | DeploymentOption | None = None,
        deployment: DeploymentOption | None = None,
        usage: TokenUsage,
        status: UsageStatus = "complete",
        provider_completed: bool | None = None,
        agent: str | None = None,
        correlation_id: str | None = None,
        timing: ChatTiming | None = None,
        billable_units: int | None = None,
        billing_unit: str | None = None,
        image_size: str | None = None,
        image_quality: str | None = None,
    ) -> None:
        """Meter one turn. Never raises: ledger/log failures are swallowed."""
        if not self._enabled:
            return
        try:
            rec = self.build_record(
                user_id=user_id,
                session_id=session_id,
                model_id=model_id,
                target=target,
                deployment=deployment,
                usage=usage,
                status=status,
                provider_completed=provider_completed,
                agent=agent,
                correlation_id=correlation_id,
                billable_units=billable_units,
                billing_unit=billing_unit,
                image_size=image_size,
                image_quality=image_quality,
            )
        except Exception:  # noqa: BLE001 - metering must never break a turn
            logger.warning("usage record build failed", exc_info=True)
            return

        timing_attributes = timing.terminal_attributes() if timing is not None else None
        self._emit_log_safe(rec, timing_attributes)
        try:
            await asyncio.shield(self._repo.record(rec))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - best-effort ledger durability
            logger.warning(
                "usage ledger write failed (correlation_id=%s)", rec.correlationId, exc_info=True
            )

    def _emit_log_safe(
        self, rec: UsageRecord, timing_attributes: dict[str, object] | None = None
    ) -> None:
        try:
            self._emit_log(rec, timing_attributes)
        except Exception:  # noqa: BLE001 - telemetry must never break a turn
            logger.warning("usage telemetry emit failed", exc_info=True)
        try:
            self._emit_event(rec, timing_attributes)
        except Exception:  # noqa: BLE001 - telemetry must never break a turn
            logger.warning("usage custom event emit failed", exc_info=True)

    def _emit_event(
        self, rec: UsageRecord, timing_attributes: dict[str, object] | None = None
    ) -> None:
        """Emit a ``chat_completion`` Application Insights customEvent so per-model
        / per-user token + cost analytics are queryable by stable pseudonymous hash
        in App Insights/KQL
        alongside the stdout signal. No-op unless telemetry is configured (i.e.
        an Application Insights connection string is set); best-effort."""
        attributes: dict[str, object] = {
            "userHash": _telemetry_identifier("user", rec.userId),
            "sessionHash": _telemetry_identifier("session", rec.sessionId),
            "provider": rec.provider,
            "model": rec.model,
            "deployment": rec.deployment,
            "target": rec.target,
            "region": rec.region,
            "agent": rec.agent,
            "status": rec.status,
            "providerCompleted": rec.providerCompleted,
            "billable": rec.billable,
            "usageKnown": rec.usageKnown,
            "usageComplete": rec.usageComplete,
            "calls": rec.calls,
            "promptTokens": rec.promptTokens,
            "completionTokens": rec.completionTokens,
            "totalTokens": rec.totalTokens,
            "billableUnits": rec.billableUnits,
            "billingUnit": rec.billingUnit,
            "imageSize": rec.imageSize,
            "imageQuality": rec.imageQuality,
            "costKnown": rec.costKnown,
            "estCostUsd": rec.estCostUsd,
            "currency": rec.currency,
            "correlationId": rec.correlationId,
        }
        if timing_attributes is not None:
            attributes.update(timing_attributes)
        emit_custom_event("chat_completion", attributes)

    def _emit_log(
        self, rec: UsageRecord, timing_attributes: dict[str, object] | None = None
    ) -> None:
        """Structured JSON telemetry line (no prompt content). Container stdout is
        shipped to Log Analytics/App Insights, so this is the queryable cost/traffic
        signal without adding an App Insights SDK dependency."""
        payload = {
            "event": "model_usage",
            "provider": rec.provider,
            "model": rec.model,
            "deployment": rec.deployment,
            "target": rec.target,
            "region": rec.region,
            "agent": rec.agent,
            "status": rec.status,
            "providerCompleted": rec.providerCompleted,
            "billable": rec.billable,
            "usageKnown": rec.usageKnown,
            "usageComplete": rec.usageComplete,
            "calls": rec.calls,
            "promptTokens": rec.promptTokens,
            "completionTokens": rec.completionTokens,
            "totalTokens": rec.totalTokens,
            "billableUnits": rec.billableUnits,
            "billingUnit": rec.billingUnit,
            "imageSize": rec.imageSize,
            "imageQuality": rec.imageQuality,
            "costKnown": rec.costKnown,
            "estCostUsd": rec.estCostUsd,
            "currency": rec.currency,
            "correlationId": rec.correlationId,
        }
        if timing_attributes is not None:
            payload.update(timing_attributes)
        try:
            logger.info(json.dumps(payload, separators=(",", ":")))
        except Exception:  # noqa: BLE001
            logger.info("model_usage model=%s status=%s", rec.model, rec.status)

    async def summarize(self, user_id: str, *, since_days: int | None = None) -> UsageSummary:
        from datetime import datetime, timedelta, timezone

        days = DEFAULT_SUMMARY_DAYS if since_days is None else max(1, min(since_days, MAX_SUMMARY_DAYS))
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)
        return await self._repo.summarize(
            user_id, since=since, since_days=days, now=now
        )

    async def summarize_session(
        self, user_id: str, session_id: str, *, limit: int = 1000
    ) -> SessionUsageSummary:
        records = await self._repo.list_for_session(
            user_id, session_id, limit=max(2, min(limit, 5000)) + 1
        )
        truncated = len(records) > limit
        records = records[:limit]
        summary = SessionUsageSummary(sessionId=session_id)
        summary.truncated = truncated
        summary.coveredRequests = len(records)
        if records:
            summary.latest = records[0]
            summary.coverageEnd = records[0].createdAt
            summary.coverageStart = records[-1].createdAt
        for record in records:
            summary.totalRequests += 1
            if record.usageKnown:
                summary.totalPromptTokens += record.promptTokens or 0
                summary.totalCompletionTokens += record.completionTokens or 0
                summary.totalTokens += record.totalTokens or 0
            else:
                summary.unknownUsageRequests += 1
            if record.costKnown and record.estCostMicroUsd is not None:
                summary.totalCostMicroUsd += record.estCostMicroUsd
            elif cost_bearing_attempt(record):
                summary.costUnknownRequests += 1
        return summary

    async def window_totals(
        self, user_id: str, *, since: datetime, now: datetime | None = None
    ) -> WindowTotals:
        """Aggregate a single user's ledger over an arbitrary ``[since, now]``
        window. Used by entitlement enforcement, which only calls this when a
        limit is actually configured (the unlimited fast path does no ledger IO).
        """
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        now = now or _dt.now(_tz.utc)
        # ``since_days`` is summary metadata only (filtering is by ``since``), so
        # any value is fine here.
        summary = await self._repo.summarize(user_id, since=since, since_days=1, now=now)
        return WindowTotals(
            requests=summary.totalRequests,
            totalTokens=summary.totalTokens,
            costMicroUsd=summary.totalCostMicroUsd,
            computeExecutions=summary.computeExecutions,
        )

    async def close(self) -> None:
        close = getattr(self._repo, "close", None)
        if close is not None:
            await close()
