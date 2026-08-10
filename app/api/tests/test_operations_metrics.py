from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai4ia_api.metrics.log_analytics import LogQueryData
from ai4ia_api.metrics.operations import (
    CHAT_LATENCY_KQL,
    DOCUMENTS_KQL,
    MEMORY_KQL,
    SECURITY_KQL,
    OperationsMetricsService,
)
from tests.conftest import make_settings


class FakeQuerier:
    def __init__(self, result: LogQueryData) -> None:
        self.result = result
        self.queries: list[tuple[str, int]] = []

    async def query(self, kql: str, *, window_minutes: int) -> LogQueryData:
        self.queries.append((kql, window_minutes))
        return self.result

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_operations_use_only_fixed_bounded_queries():
    now = datetime.now(timezone.utc)
    fake = FakeQuerier(
        LogQueryData(
            rows=[
                {
                    "route": "POST /api/chat",
                    "requests": 3,
                    "sourceTimestamp": now,
                }
            ]
        )
    )
    service = OperationsMetricsService(make_settings(), querier=fake)
    report = await service.operations(window_minutes=60)
    assert report.windowMinutes == 60
    assert len(fake.queries) == len(report.panels)
    assert all(minutes == 60 for _, minutes in fake.queries)
    assert all("| summarize" in query for query, _ in fake.queries)
    assert all(panel.status in {"ok", "partial"} for panel in report.panels)
    assert next(panel for panel in report.panels if panel.key == "memory").status == "partial"


@pytest.mark.asyncio
async def test_unconfigured_workspace_is_explicitly_unavailable():
    service = OperationsMetricsService(
        make_settings(log_analytics_workspace_id=None)
    )
    report = await service.security(window_minutes=60)
    assert report.panels[0].status == "unavailable"
    assert "not configured" in (report.panels[0].reason or "")


@pytest.mark.asyncio
async def test_partial_and_stale_states_are_not_zero_shaped():
    old = datetime.now(timezone.utc) - timedelta(hours=4)
    fake = FakeQuerier(
        LogQueryData(
            rows=[{"events": 2, "sourceTimestamp": old}],
            partial=True,
            reason="workspace returned a partial result",
        )
    )
    service = OperationsMetricsService(make_settings(), querier=fake)
    report = await service.security(window_minutes=60)
    panel = report.panels[0]
    assert panel.status == "stale"
    assert panel.rows == [{"events": 2, "sourceTimestamp": old}]
    assert panel.lagSeconds is not None and panel.lagSeconds > 0


@pytest.mark.asyncio
async def test_missing_terminal_telemetry_is_partial_not_zero():
    service = OperationsMetricsService(
        make_settings(), querier=FakeQuerier(LogQueryData(rows=[]))
    )
    report = await service.operations(window_minutes=60)
    documents = next(panel for panel in report.panels if panel.key == "documents")
    memory = next(panel for panel in report.panels if panel.key == "memory")
    assert documents.status == "partial" and documents.rows == []
    assert memory.status == "partial" and memory.rows == []
    assert "No matching telemetry" in (documents.reason or "")


def test_existing_workspace_is_wired_without_new_resource_or_rbac():
    root = Path(__file__).resolve().parents[3]
    monitoring = (root / "infra/modules/monitoring.bicep").read_text()
    api = (root / "infra/modules/api.bicep").read_text()
    main = (root / "infra/main.bicep").read_text()
    assert "output logAnalyticsCustomerId" in monitoring
    assert "AI4IA_LOG_ANALYTICS_WORKSPACE_ID" in api
    assert "AI4IA_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID" in api
    assert "monitoring.outputs.logAnalyticsCustomerId" in main
    assert "Microsoft.OperationalInsights/workspaces" not in api


def test_terminal_and_security_queries_match_real_custom_events():
    assert 'Name == "document_ingest_terminal"' in DOCUMENTS_KQL
    assert 'Name == "memory_operation"' in MEMORY_KQL
    assert 'Name == "security_block"' in SECURITY_KQL
    assert "AppEvents" in SECURITY_KQL
    assert "AppTraces" not in SECURITY_KQL


def test_chat_latency_query_reports_historical_gaps_as_unavailable_not_nan():
    assert 'Name == "chat_completion"' in CHAT_LATENCY_KQL
    assert "timingCovered == 0" in CHAT_LATENCY_KQL
    assert '"unavailable"' in CHAT_LATENCY_KQL
    assert "isnotnull(turnTotalMs)" in CHAT_LATENCY_KQL
    assert "NaN" not in CHAT_LATENCY_KQL


@pytest.mark.asyncio
async def test_close_swallows_querier_close_failure():
    """A querier.close() failure must not propagate: main.py's shutdown block
    closes every resource independently, so one raising close() must not be
    allowed to look like the service itself is broken."""

    class RaisingQuerier(FakeQuerier):
        async def close(self) -> None:
            raise RuntimeError("boom")

    service = OperationsMetricsService(
        make_settings(), querier=RaisingQuerier(LogQueryData(rows=[]))
    )
    await service.close()  # must not raise
