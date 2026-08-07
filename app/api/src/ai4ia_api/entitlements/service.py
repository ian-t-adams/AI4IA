"""EntitlementService: effective-policy resolution + per-turn enforcement.

Design goals (see package docstring):
- **Unlimited is free.** A not-disabled user with no limits returns *allow* with
  zero ledger IO. The only work for the common case is a cached store lookup.
- **Cheap hot path.** Effective entitlements are cached per user behind a short
  TTL, so unlimited users do not hit Cosmos on every turn; an admin change
  propagates within the TTL (and immediately on the replica that made it).
- **Fail open, except disabled.** Ledger/store errors return *allow* (availability
  over strict caps). An explicitly ``disabled`` account stays blocked from the
  last-known cached entitlement even if the store is momentarily unreachable.
- **Soft enforcement.** The check reads the ledger before a turn is metered, so
  concurrent turns can overshoot a limit slightly. This is intentional for a
  personal/demo app; it is not a strict quota.
- **Budgets are only as strong as the ledger.** Token totals count only
  usage-known turns and cost totals only price-known turns (honesty
  model), so a model that stops reporting usage, or a missing price, makes
  token/cost budgets under-count (fail open). Rate limits (request counts) and
  ``disabled`` do not depend on usage/pricing completeness. The startup guard in
  ``Settings.validate_runtime`` additionally refuses enforcement with metering
  off, so positive limits can never silently no-op for lack of a ledger. That
  guard covers ``computeExecutionsPerDay`` for free: sandbox executions accrue
  in the same ledger, so with metering off the compute counter would stay at 0
  exactly like the token/cost ones.
- **Scoped limits stay scoped.** ``computeExecutionsPerDay`` is enforced only on
  the ``compute`` scope, so exhausting a sandbox allowance never blocks ordinary
  chat — and a user whose *only* limit is that one still takes a chat turn with
  zero ledger IO (the daily read is skipped when no daily limit applies to the
  scope being checked).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol

from .models import (
    DAY_SECONDS,
    MINUTE_SECONDS,
    MONTH_SECONDS,
    Entitlement,
    EntitlementDecision,
    EntitlementLimits,
    EntitlementScope,
)
from .store import EntitlementStore

if TYPE_CHECKING:
    from ..usage.models import WindowTotals

logger = logging.getLogger(__name__)


class UsageWindowReader(Protocol):
    """Just enough of :class:`~ai4ia_api.usage.service.UsageService` to enforce
    budgets, so the service depends on a tiny structural interface (and tests can
    pass a fake reader)."""

    async def window_totals(
        self, user_id: str, *, since: datetime, now: datetime | None = None
    ) -> "WindowTotals": ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _CacheEntry:
    # ``None`` value caches a *negative* lookup (the user has no override).
    value: Entitlement | None
    expires_monotonic: float


class EntitlementService:
    def __init__(
        self,
        store: EntitlementStore,
        usage: UsageWindowReader,
        default: Entitlement,
        *,
        enabled: bool = True,
        cache_ttl_seconds: int = 30,
    ) -> None:
        self._store = store
        self._usage = usage
        self._default = default
        self._enabled = enabled
        self._cache_ttl = max(0, cache_ttl_seconds)
        self._cache: dict[str, _CacheEntry] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def default(self) -> Entitlement:
        return self._default

    # ---- effective-policy resolution (cached) ----

    def _cache_put(self, user_id: str, override: Entitlement | None) -> None:
        self._cache[user_id] = _CacheEntry(
            value=override, expires_monotonic=time.monotonic() + self._cache_ttl
        )

    async def get_effective(self, user_id: str) -> Entitlement:
        """The override for ``user_id`` if present, else the default. Cached
        behind a short TTL. On a store error, falls back to the last-known cached
        value **only when it is ``disabled``** (a deliberate block must survive a
        transient outage); for any other cached or missing value it fails OPEN to
        the unlimited default, so a store hiccup never invents a numeric limit."""
        entry = self._cache.get(user_id)
        if entry is not None and entry.expires_monotonic > time.monotonic():
            return entry.value or self._default
        try:
            override = await self._store.get(user_id)
            self._cache_put(user_id, override)
            return override or self._default
        except Exception:  # noqa: BLE001 - store hiccup must not break the turn
            logger.warning("entitlement store get failed (user=%s)", user_id, exc_info=True)
            # Fail open EXCEPT for a known-disabled account.
            if entry is not None and entry.value is not None and entry.value.disabled:
                return entry.value
            return self._default

    # ---- admin mutation (write-through cache) ----

    async def set(self, user_id: str, limits: EntitlementLimits, *, updated_by: str | None) -> Entitlement:
        ent = Entitlement(
            id=user_id,
            userId=user_id,
            updatedAt=_now(),
            updatedBy=updated_by,
            **limits.model_dump(),
        )
        await self._store.put(ent)
        self._cache_put(user_id, ent)
        return ent

    async def clear(self, user_id: str) -> None:
        await self._store.delete(user_id)
        self._cache_put(user_id, None)

    async def list_overrides(self) -> list[Entitlement]:
        return await self._store.list()

    async def close(self) -> None:
        close = getattr(self._store, "close", None)
        if close is not None:
            await close()

    # ---- per-turn enforcement ----

    async def check(
        self, user_id: str, *, scope: EntitlementScope = "chat"
    ) -> EntitlementDecision:
        """Decide whether ``user_id`` may consume a model turn right now.

        ``scope`` selects which limits apply. ``chat`` (the default, and what
        every HTTP router passes) is unchanged from before this parameter
        existed. ``compute`` additionally enforces ``computeExecutionsPerDay``
        and is what the Code Interpreter capabilities call before each sandbox
        execution.
        """
        if not self._enabled:
            return EntitlementDecision.allow()

        ent = await self.get_effective(user_id)

        # Unlimited fast path: the common case does no ledger IO.
        if ent.is_unlimited:
            return EntitlementDecision.allow()
        if ent.disabled:
            return EntitlementDecision(
                allowed=False,
                code=403,
                reason="This account is not permitted to send chat messages.",
                limit_kind="disabled",
            )

        try:
            return await self._check_budgets(user_id, ent, scope)
        except Exception:  # noqa: BLE001 - ledger failure fails OPEN (availability)
            logger.warning(
                "entitlement budget check failed; allowing (user=%s)", user_id, exc_info=True
            )
            return EntitlementDecision.allow()

    async def _check_budgets(
        self, user_id: str, ent: Entitlement, scope: EntitlementScope = "chat"
    ) -> EntitlementDecision:
        now = _now()
        # Only the compute scope spends the sandbox allowance, so a chat turn for
        # a user whose only limit is that one still does zero ledger IO.
        compute_cap = ent.computeExecutionsPerDay if scope == "compute" else None

        # Rolling request-rate limit (last 60s).
        if ent.requestsPerMinute is not None:
            totals = await self._usage.window_totals(
                user_id, since=now - timedelta(seconds=MINUTE_SECONDS), now=now
            )
            if totals.requests >= ent.requestsPerMinute:
                return self._deny(
                    "requests_per_minute", ent.requestsPerMinute, totals.requests,
                    MINUTE_SECONDS, "Rate limit exceeded. Please slow down.",
                )

        # Rolling daily budgets (last 24h) — one ledger read covers all three.
        if (
            ent.tokensPerDay is not None
            or ent.costPerDayMicroUsd is not None
            or compute_cap is not None
        ):
            totals = await self._usage.window_totals(
                user_id, since=now - timedelta(seconds=DAY_SECONDS), now=now
            )
            if ent.tokensPerDay is not None and totals.totalTokens >= ent.tokensPerDay:
                return self._deny(
                    "tokens_per_day", ent.tokensPerDay, totals.totalTokens,
                    DAY_SECONDS, "Daily token budget reached.",
                )
            if ent.costPerDayMicroUsd is not None and totals.costMicroUsd >= ent.costPerDayMicroUsd:
                return self._deny(
                    "cost_per_day", ent.costPerDayMicroUsd, totals.costMicroUsd,
                    DAY_SECONDS, "Daily cost budget reached.",
                )
            if compute_cap is not None and totals.computeExecutions >= compute_cap:
                return self._deny(
                    "compute_executions_per_day", compute_cap, totals.computeExecutions,
                    DAY_SECONDS, "Daily code-execution budget reached.",
                )

        # Rolling monthly budgets (last 30d).
        if ent.tokensPerMonth is not None or ent.costPerMonthMicroUsd is not None:
            totals = await self._usage.window_totals(
                user_id, since=now - timedelta(seconds=MONTH_SECONDS), now=now
            )
            if ent.tokensPerMonth is not None and totals.totalTokens >= ent.tokensPerMonth:
                return self._deny(
                    "tokens_per_month", ent.tokensPerMonth, totals.totalTokens,
                    MONTH_SECONDS, "Monthly token budget reached.",
                )
            if ent.costPerMonthMicroUsd is not None and totals.costMicroUsd >= ent.costPerMonthMicroUsd:
                return self._deny(
                    "cost_per_month", ent.costPerMonthMicroUsd, totals.costMicroUsd,
                    MONTH_SECONDS, "Monthly cost budget reached.",
                )

        return EntitlementDecision.allow()

    @staticmethod
    def _deny(
        kind: str, limit: int, used: int, retry_after: int, reason: str
    ) -> EntitlementDecision:
        # Coarse Retry-After (window size, capped at a day) is plenty for a soft
        # limit; an exact "oldest record ages out" hint isn't worth the ledger detail.
        return EntitlementDecision(
            allowed=False,
            code=429,
            reason=reason,
            retry_after_seconds=min(retry_after, DAY_SECONDS),
            limit_kind=kind,
            limit=limit,
            used=used,
        )
