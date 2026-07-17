"""Bounded, read-only Log Analytics query adapter using managed identity."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class LogQueryData:
    rows: list[dict[str, Any]]
    partial: bool = False
    reason: str | None = None


@runtime_checkable
class LogAnalyticsQuerier(Protocol):
    async def query(self, kql: str, *, window_minutes: int) -> LogQueryData: ...
    async def close(self) -> None: ...


class AzureLogAnalyticsQuerier:
    def __init__(self, workspace_id: str) -> None:
        if not workspace_id:
            raise ValueError("Log Analytics workspace id is required")
        from azure.identity.aio import DefaultAzureCredential
        from azure.monitor.query.aio import LogsQueryClient

        self._workspace_id = workspace_id
        self._credential = DefaultAzureCredential()
        self._client = LogsQueryClient(self._credential)

    async def query(self, kql: str, *, window_minutes: int) -> LogQueryData:
        from azure.monitor.query import LogsQueryStatus

        response = await self._client.query_workspace(
            self._workspace_id,
            kql,
            timespan=timedelta(minutes=window_minutes),
            server_timeout=30,
        )
        partial = response.status == LogsQueryStatus.PARTIAL
        tables = response.partial_data if partial else response.tables
        rows: list[dict[str, Any]] = []
        for table in tables:
            columns = [
                str(getattr(column, "name", column))
                for column in table.columns
            ]
            rows.extend(dict(zip(columns, values, strict=False)) for values in table.rows)
        reason = None
        if partial:
            error = getattr(response, "partial_error", None)
            reason = str(getattr(error, "message", error) or "Partial query result")
        return LogQueryData(rows=rows, partial=partial, reason=reason)

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()
