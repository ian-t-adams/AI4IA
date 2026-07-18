"""Fixed-KQL operational and security reports over the existing workspace."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from .log_analytics import AzureLogAnalyticsQuerier, LogAnalyticsQuerier
from .models import OperationalMetricsReport, OperationalPanel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuerySpec:
    key: str
    display_name: str
    source: str
    kql: str
    required_values: tuple[str, ...] = ()
    dimension_key: str | None = None


REQUESTS_KQL = """
AppRequests
| summarize requests=count(), errors=countif(Success == false),
    p50Ms=percentile(DurationMs, 50), p95Ms=percentile(DurationMs, 95),
    p99Ms=percentile(DurationMs, 99), sourceTimestamp=max(TimeGenerated)
  by route=Name
| top 50 by requests desc
"""

DEPENDENCIES_KQL = """
AppDependencies
| summarize calls=count(), failures=countif(Success == false),
    p50Ms=percentile(DurationMs, 50), p95Ms=percentile(DurationMs, 95),
    p99Ms=percentile(DurationMs, 99), sourceTimestamp=max(TimeGenerated)
  by dependency=Name, target=Target, dependencyType=Type
| top 50 by calls desc
"""

VOICE_KQL = """
AppEvents
| where Name == "voice_live_completion"
| extend provider=tostring(Properties["provider"]), model=tostring(Properties["model"]),
    outcome=tostring(Properties["outcome"]), closeCode=tostring(Properties["closeCode"]),
    durationMs=tolong(Properties["durationMs"])
| summarize sessions=count(), errors=countif(outcome == "error"),
    p50DurationMs=percentile(durationMs, 50), p95DurationMs=percentile(durationMs, 95),
    sourceTimestamp=max(TimeGenerated)
  by provider, model, outcome, closeCode
| top 50 by sessions desc
"""

TOOLS_KQL = """
AppEvents
| where Name in ("mcp_tool_call", "tool_authorization")
| extend tool=tostring(Properties["tool"]), outcome=tostring(Properties["outcome"]),
    source=tostring(Properties["source"]), latencyMs=tolong(Properties["latencyMs"])
| summarize calls=count(), failures=countif(outcome !in ("ok", "approved")),
    approvals=countif(outcome == "approved"), denials=countif(outcome == "denied"),
    p95LatencyMs=percentile(latencyMs, 95), sourceTimestamp=max(TimeGenerated)
  by source, tool, outcome
| top 100 by calls desc
"""

DOCUMENTS_KQL = """
AppEvents
| where Name == "document_ingest_terminal"
| extend status=tostring(Properties["status"]), modality=tostring(Properties["modality"]),
    stage=tostring(Properties["stage"]),
    persistenceOutcome=tostring(Properties["persistenceOutcome"]),
    latencyMs=tolong(Properties["latencyMs"])
| summarize operations=count(), failures=countif(status == "failed"),
    p95LatencyMs=percentile(latencyMs, 95), sourceTimestamp=max(TimeGenerated)
  by status, modality, stage, persistenceOutcome
"""

MEMORY_KQL = """
AppEvents
| where Name == "memory_operation"
| extend operation=tostring(Properties["operation"]), status=tostring(Properties["status"]),
    source=tostring(Properties["source"]),
    latencyMs=tolong(Properties["latencyMs"])
| summarize operations=count(), failures=countif(status == "failed"),
    p95LatencyMs=percentile(latencyMs, 95), sourceTimestamp=max(TimeGenerated)
  by operation, status, source
"""

USAGE_KQL = """
AppEvents
| where Name == "chat_completion"
| extend provider=tostring(Properties["provider"]), model=tostring(Properties["model"]),
    status=tostring(Properties["status"]), usageKnown=tobool(Properties["usageKnown"]),
    costKnown=tobool(Properties["costKnown"]), totalTokens=tolong(Properties["totalTokens"]),
    estCostUsd=todouble(Properties["estCostUsd"])
| summarize requests=count(), tokens=sum(totalTokens),
    knownCostUsd=sum(estCostUsd),
    unknownUsage=countif(usageKnown == false), unknownCost=countif(costKnown == false),
    sourceTimestamp=max(TimeGenerated)
  by provider, model, status
| top 100 by requests desc
"""

SECURITY_KQL = """
AppEvents
| where Name == "security_block"
| extend category=tostring(Properties["category"]),
    reason=tostring(Properties["reason"]), source=tostring(Properties["source"])
| summarize events=count(), sourceTimestamp=max(TimeGenerated)
  by category, reason, source
| top 100 by events desc
"""

OPERATION_SPECS = (
    QuerySpec("requests", "Requests and route latency", "Application Insights requests", REQUESTS_KQL),
    QuerySpec("dependencies", "Dependencies and stage latency", "Application Insights dependencies", DEPENDENCIES_KQL),
    QuerySpec("voice", "Realtime voice", "AI4IA metadata events", VOICE_KQL),
    QuerySpec("tools", "Tools and MCP", "AI4IA metadata events", TOOLS_KQL),
    QuerySpec("documents", "Document ingestion", "AI4IA terminal metadata events", DOCUMENTS_KQL),
    QuerySpec(
        "memory",
        "Memory operations",
        "AI4IA metadata events",
        MEMORY_KQL,
        ("list", "delete", "recall", "save"),
        "operation",
    ),
    QuerySpec("usage", "Model usage coverage", "AI4IA usage events", USAGE_KQL),
)
SECURITY_SPECS = (
    QuerySpec(
        "security",
        "Security and governance blocks",
        "AI4IA security metadata events",
        SECURITY_KQL,
        ("http_auth", "admin_auth", "tool_authorization", "ssrf", "realtime_auth"),
        "category",
    ),
)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except ValueError:
            return None
    return None


class OperationsMetricsService:
    def __init__(
        self,
        settings: Settings,
        *,
        querier: LogAnalyticsQuerier | None = None,
    ) -> None:
        self._settings = settings
        self._querier = querier
        self._querier_unavailable = False

    async def operations(self, *, window_minutes: int) -> OperationalMetricsReport:
        return await self._report(OPERATION_SPECS, window_minutes)

    async def security(self, *, window_minutes: int) -> OperationalMetricsReport:
        return await self._report(SECURITY_SPECS, window_minutes)

    async def _report(
        self, specs: tuple[QuerySpec, ...], window_minutes: int
    ) -> OperationalMetricsReport:
        report = OperationalMetricsReport(
            windowMinutes=window_minutes,
            diagnosticsUrl=self._diagnostics_url(),
        )
        querier = await self._get_querier()
        if querier is None:
            reason = (
                "Log Analytics workspace is not configured or the query client "
                "is unavailable."
            )
            report.panels = [
                OperationalPanel(
                    key=spec.key,
                    displayName=spec.display_name,
                    status="unavailable",
                    source=spec.source,
                    reason=reason,
                )
                for spec in specs
            ]
            return report
        for spec in specs:
            report.panels.append(
                await self._panel(querier, spec, window_minutes)
            )
        return report

    async def _panel(
        self,
        querier: LogAnalyticsQuerier,
        spec: QuerySpec,
        window_minutes: int,
    ) -> OperationalPanel:
        try:
            result = await querier.query(spec.kql, window_minutes=window_minutes)
        except Exception:  # noqa: BLE001 - degrade this panel, not the whole report
            logger.warning("Log Analytics query failed panel=%s", spec.key, exc_info=True)
            return OperationalPanel(
                key=spec.key,
                displayName=spec.display_name,
                status="unavailable",
                source=spec.source,
                reason="Query failed or the managed identity cannot read the workspace.",
            )
        timestamps = [
            timestamp
            for row in result.rows
            if (timestamp := _as_datetime(row.get("sourceTimestamp"))) is not None
        ]
        source_timestamp = max(timestamps) if timestamps else None
        lag = (
            int((datetime.now(timezone.utc) - source_timestamp).total_seconds())
            if source_timestamp
            else None
        )
        status = "partial" if result.partial or not result.rows else "ok"
        reason = result.reason
        if not result.rows and reason is None:
            reason = "No matching telemetry in this window."
        if result.rows and spec.dimension_key and spec.required_values:
            observed = {
                str(row.get(spec.dimension_key))
                for row in result.rows
                if row.get(spec.dimension_key) is not None
            }
            missing = [value for value in spec.required_values if value not in observed]
            if missing:
                status = "partial"
                reason = f"Missing telemetry categories: {', '.join(missing)}."
        # Staleness is ingestion freshness, independent of the selected history
        # window. A matching event older than 15 minutes is stale even in a 24h view.
        if lag is not None and lag > 900:
            status = "stale"
            reason = "Latest matching telemetry is stale."
        return OperationalPanel(
            key=spec.key,
            displayName=spec.display_name,
            status=status,
            source=spec.source,
            sourceTimestamp=source_timestamp,
            lagSeconds=lag,
            reason=reason,
            rows=result.rows,
        )

    async def _get_querier(self) -> LogAnalyticsQuerier | None:
        if self._querier is not None:
            return self._querier
        if self._querier_unavailable:
            return None
        workspace_id = self._settings.log_analytics_workspace_id
        if not workspace_id:
            self._querier_unavailable = True
            return None
        try:
            self._querier = AzureLogAnalyticsQuerier(workspace_id)
        except Exception:  # noqa: BLE001 - treat as "unavailable", not fatal
            logger.warning("Log Analytics client unavailable", exc_info=True)
            self._querier_unavailable = True
            return None
        return self._querier

    def _diagnostics_url(self) -> str | None:
        resource_id = self._settings.log_analytics_workspace_resource_id
        return (
            f"https://portal.azure.com/#resource{resource_id}/logs"
            if resource_id
            else None
        )

    async def close(self) -> None:
        if self._querier is not None:
            try:
                await self._querier.close()
            except Exception:  # noqa: BLE001
                logger.warning("Log Analytics querier close failed", exc_info=True)
