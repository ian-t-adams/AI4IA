"""UsageRepository protocol + shared error.

The ledger is partitioned by ``userId`` (matching the sessions store), so every
per-user read is naturally user-scoped and bounded to a single partition.
``record`` is fire-and-forget from the caller's perspective (the service shields
it); a failing ledger write must never break a chat response.

``query_records`` is the admin-only exception: a *cross-partition*, time-bounded,
record-capped scan over the whole ledger used by the admin aggregation API. It is
read-only and best-effort, and its ``limit`` caps the RU/rows a single dashboard
request can ever consume.

``query_rollup_rows`` is the same scan with a *projection*: it returns only the
fields the admin rollups actually read (:data:`~.models.ROLLUP_FIELDS`), as slim
:class:`~.models.UsageRollupRow` values. Same window, same cap, same
cross-partition posture — materially less memory and response payload per row,
which is what keeps a full 50,000-row admin window inside the API's memory limit.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import UsageRecord, UsageRollupRow, UsageSummary


@runtime_checkable
class UsageRepository(Protocol):
    async def record(self, record: UsageRecord) -> None: ...

    async def summarize(
        self, user_id: str, *, since: datetime, since_days: int, now: datetime
    ) -> UsageSummary: ...

    async def list_for_session(
        self, user_id: str, session_id: str, *, limit: int
    ) -> list[UsageRecord]: ...

    async def query_records(
        self, *, since: datetime, now: datetime, limit: int
    ) -> list[UsageRecord]: ...

    async def query_rollup_rows(
        self, *, since: datetime, now: datetime, limit: int
    ) -> list[UsageRollupRow]: ...

    async def close(self) -> None: ...
