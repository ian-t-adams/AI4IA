"""Usage metering service: build a ledger record, estimate cost, persist + log.

This sits at the chat layer (which knows the user, model, deployment and agent)
and is the single place that turns a completed turn's :class:`TokenUsage` into a
durable :class:`UsageRecord`. Persistence is best-effort and self-contained: a
failing ledger write or price lookup never propagates to the chat response.

Cost honesty rules (see ``usage`` package docstring):
- A turn is ``billable`` only when ``status == "complete"`` AND usage was both
  known and complete (every contributing model call reported usage).
- Cost is estimated only for billable turns with a known price; otherwise the
  record is ``costKnown == False`` and the summary counts it as cost-unknown.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from ..catalog import DeploymentOption
from .models import TokenUsage, UsageRecord, UsageStatus, UsageSummary, WindowTotals
from .pricing import PricingBook
from .repository import UsageRepository

logger = logging.getLogger(__name__)

# Bound the summary window so a query can never scan an unbounded history.
DEFAULT_SUMMARY_DAYS = 30
MAX_SUMMARY_DAYS = 90


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

    def build_record(
        self,
        *,
        user_id: str,
        session_id: str,
        model_id: str,
        deployment: DeploymentOption,
        usage: TokenUsage,
        status: UsageStatus,
        agent: str | None,
        correlation_id: str | None,
    ) -> UsageRecord:
        billable = status == "complete" and usage.known and usage.complete
        rec = UsageRecord(
            userId=user_id,
            sessionId=session_id,
            model=model_id,
            deployment=deployment.deploymentName,
            region=deployment.region,
            dataZone=deployment.dataZone,
            agent=agent,
            status=status,
            billable=billable,
            usageKnown=usage.known,
            usageComplete=usage.complete,
            calls=usage.calls,
            promptTokens=usage.prompt if usage.known else None,
            completionTokens=usage.completion if usage.known else None,
            totalTokens=usage.total if usage.known else None,
            correlationId=correlation_id,
        )
        # Estimate cost only for a billable turn with real, complete usage.
        if billable:
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
        deployment: DeploymentOption,
        usage: TokenUsage,
        status: UsageStatus = "complete",
        agent: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Meter one turn. Never raises: ledger/log failures are swallowed."""
        if not self._enabled:
            return
        try:
            rec = self.build_record(
                user_id=user_id,
                session_id=session_id,
                model_id=model_id,
                deployment=deployment,
                usage=usage,
                status=status,
                agent=agent,
                correlation_id=correlation_id,
            )
        except Exception:  # noqa: BLE001 - metering must never break a turn
            logger.warning("usage record build failed", exc_info=True)
            return

        self._emit_log_safe(rec)
        try:
            await asyncio.shield(self._repo.record(rec))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - best-effort ledger durability
            logger.warning(
                "usage ledger write failed (correlation_id=%s)", rec.correlationId, exc_info=True
            )

    def _emit_log_safe(self, rec: UsageRecord) -> None:
        try:
            self._emit_log(rec)
        except Exception:  # noqa: BLE001 - telemetry must never break a turn
            logger.warning("usage telemetry emit failed", exc_info=True)

    def _emit_log(self, rec: UsageRecord) -> None:
        """Structured JSON telemetry line (no prompt content). Container stdout is
        shipped to Log Analytics/App Insights, so this is the queryable cost/traffic
        signal without adding an App Insights SDK dependency."""
        payload = {
            "event": "model_usage",
            "userId": rec.userId,
            "sessionId": rec.sessionId,
            "model": rec.model,
            "deployment": rec.deployment,
            "region": rec.region,
            "agent": rec.agent,
            "status": rec.status,
            "billable": rec.billable,
            "usageKnown": rec.usageKnown,
            "usageComplete": rec.usageComplete,
            "calls": rec.calls,
            "promptTokens": rec.promptTokens,
            "completionTokens": rec.completionTokens,
            "totalTokens": rec.totalTokens,
            "costKnown": rec.costKnown,
            "estCostUsd": rec.estCostUsd,
            "currency": rec.currency,
            "correlationId": rec.correlationId,
        }
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
        )

    async def close(self) -> None:
        close = getattr(self._repo, "close", None)
        if close is not None:
            await close()
