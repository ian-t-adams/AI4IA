from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai4ia_api.metrics.log_analytics import LogQueryData
from ai4ia_api.metrics.operations import OperationsMetricsService
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
    assert all(panel.status == "ok" for panel in report.panels)


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
