"""Entitlement data model + the per-turn enforcement decision.

``Entitlement`` is the per-user policy. Every numeric limit is optional and
defaults to ``None`` == *unlimited*; a value of ``0`` is a deliberate hard block
for that dimension; negatives are rejected. ``disabled`` blocks model-backed
chat outright (local ``/commands`` still work — see the router/chat docs).

Budgets/limits use **rolling** windows (last 60s / 24h / 30d) rather than
calendar day/month, which sidesteps timezone and reset-boundary bugs and matches
how the ledger is queried (``createdAt >= since``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)

# Rolling-window sizes (seconds).
MINUTE_SECONDS = 60
DAY_SECONDS = 24 * 60 * 60
MONTH_SECONDS = 30 * DAY_SECONDS


class EntitlementLimits(BaseModel):
    """The settable limits, shared by the stored record and the admin DTO.

    ``None`` everywhere (the default) == fully unlimited. Each numeric field is
    ``ge=0`` so ``0`` is a valid hard block and negatives are rejected at the
    edge.
    """

    disabled: bool = False
    requestsPerMinute: int | None = Field(default=None, ge=0)
    tokensPerDay: int | None = Field(default=None, ge=0)
    costPerDayMicroUsd: int | None = Field(default=None, ge=0)
    tokensPerMonth: int | None = Field(default=None, ge=0)
    costPerMonthMicroUsd: int | None = Field(default=None, ge=0)
    note: str | None = None

    @property
    def has_any_limit(self) -> bool:
        """True when at least one numeric budget/rate limit is set.

        ``disabled`` is intentionally excluded: it is checked on its own path so
        the unlimited fast path can still short-circuit a not-disabled user.
        """
        return any(
            v is not None
            for v in (
                self.requestsPerMinute,
                self.tokensPerDay,
                self.costPerDayMicroUsd,
                self.tokensPerMonth,
                self.costPerMonthMicroUsd,
            )
        )

    @property
    def is_unlimited(self) -> bool:
        return not self.disabled and not self.has_any_limit


class Entitlement(EntitlementLimits):
    """A user's effective policy as persisted in the ``entitlements`` store
    (Cosmos PK ``/userId``, one doc per user, ``id == userId``)."""

    id: str
    userId: str
    updatedAt: datetime = Field(default_factory=_now)
    updatedBy: str | None = None

    @classmethod
    def unlimited(cls, user_id: str = "") -> "Entitlement":
        return cls(id=user_id, userId=user_id)


# A per-turn decision. Internal (never serialized to a client as-is); the router
# maps a denial onto an HTTPException with the right status + Retry-After.
@dataclass(frozen=True)
class EntitlementDecision:
    allowed: bool
    code: int = 200
    reason: str | None = None
    retry_after_seconds: int | None = None
    limit_kind: str | None = None
    limit: int | None = None
    used: int | None = None

    @classmethod
    def allow(cls) -> "EntitlementDecision":
        return cls(allowed=True)
