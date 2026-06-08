"""In-memory usage ledger for local/dev/tests (not durable across restarts)."""
from __future__ import annotations

from datetime import datetime

from .models import UsageRecord, UsageSummary, summarize_records


class InMemoryUsageRepository:
    def __init__(self) -> None:
        self._by_user: dict[str, list[UsageRecord]] = {}

    async def record(self, record: UsageRecord) -> None:
        self._by_user.setdefault(record.userId, []).append(record)

    async def summarize(
        self, user_id: str, *, since: datetime, since_days: int, now: datetime
    ) -> UsageSummary:
        records = [
            r for r in self._by_user.get(user_id, []) if r.createdAt >= since
        ]
        return summarize_records(
            user_id, records, since_days=since_days, from_time=since, to_time=now
        )

    async def close(self) -> None:
        return None
