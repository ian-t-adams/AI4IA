"""In-memory usage ledger for local/dev/tests (not durable across restarts)."""
from __future__ import annotations

from datetime import datetime

from .models import UsageRecord, UsageRollupRow, UsageSummary, summarize_records


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

    async def query_records(
        self, *, since: datetime, now: datetime, limit: int
    ) -> list[UsageRecord]:
        """Cross-user, time-bounded scan for admin aggregation. Newest first, and
        capped at ``limit`` rows to mirror the Cosmos repo's bounded contract."""
        records = [
            r
            for recs in self._by_user.values()
            for r in recs
            if since <= r.createdAt <= now
        ]
        records.sort(key=lambda r: r.createdAt, reverse=True)
        if limit >= 0:
            records = records[:limit]
        return records

    async def query_rollup_rows(
        self, *, since: datetime, now: datetime, limit: int
    ) -> list[UsageRollupRow]:
        """Projected form of :meth:`query_records` (same window, order and cap).

        The in-memory ledger already holds full records, so this projects rather
        than re-queries — it exists so local/dev and tests exercise the exact code
        path production takes, and so repo/aggregate parity is testable.
        """
        return [
            UsageRollupRow.from_record(record)
            for record in await self.query_records(since=since, now=now, limit=limit)
        ]

    async def list_for_session(
        self, user_id: str, session_id: str, *, limit: int
    ) -> list[UsageRecord]:
        records = [
            record
            for record in self._by_user.get(user_id, [])
            if record.sessionId == session_id
        ]
        records.sort(key=lambda record: record.createdAt, reverse=True)
        return records[: max(0, limit)]

    async def close(self) -> None:
        return None
