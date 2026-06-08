"""UsageRepository protocol + shared error.

The ledger is partitioned by ``userId`` (matching the sessions store), so every
read is naturally user-scoped and bounded to a single partition. ``record`` is
fire-and-forget from the caller's perspective (the service shields it); a failing
ledger write must never break a chat response.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import UsageRecord, UsageSummary


@runtime_checkable
class UsageRepository(Protocol):
    async def record(self, record: UsageRecord) -> None: ...

    async def summarize(
        self, user_id: str, *, since: datetime, since_days: int, now: datetime
    ) -> UsageSummary: ...

    async def close(self) -> None: ...
