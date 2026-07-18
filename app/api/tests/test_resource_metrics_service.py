"""Regression coverage for ResourceMetricsService's per-panel Azure Monitor
query parameters.

Production incident: the admin resource-metrics dashboard's Cosmos DB panel
failed against Azure Monitor's batch metrics API with

    BadRequest: ... TotalRequestUnits, TotalRequests, ServiceAvailability
    only support common time grain 01:00:00 ...

Every panel was queried at ``ResourceMetricsService``'s single default
granularity (5 minutes), but Cosmos DB's ``ServiceAvailability`` metric only
supports a 1-hour time grain (``TotalRequests``/``TotalRequestUnits`` support
down to 1 minute -- see the "Microsoft.DocumentDB/databaseAccounts" entry in
Azure Monitor's supported-metrics reference). The batch API requires every
metric in one call to share a common supported grain, so combining all three
Cosmos metrics at 5 minutes is rejected outright, every time the panel is
unset. These tests pin the fix: the Cosmos panel must request its own
1-hour-compatible granularity while every other panel is unaffected.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ai4ia_api.metrics.models import MetricPoint, MetricRequest
from ai4ia_api.metrics.service import (
    DEFAULT_GRANULARITY_MINUTES,
    PANEL_SPECS,
    ResourceMetricsService,
)
from tests.conftest import make_settings

_SUB = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"


@dataclass
class _RecordingQuerier:
    """Fake ``MetricsQuerier`` that records the window/granularity used for
    each call and echoes back a placeholder point per request (panel content
    isn't what's under test here)."""

    calls: list[tuple[str, int, int]] = field(default_factory=list)

    async def query(
        self,
        resource_id: str,
        requests: list[MetricRequest],
        *,
        window_minutes: int,
        granularity_minutes: int,
    ) -> list[MetricPoint]:
        self.calls.append((resource_id, window_minutes, granularity_minutes))
        return [
            MetricPoint(name=r.name, label=r.label, aggregation=r.aggregation, value=1.0, unit=r.unit)
            for r in requests
        ]

    async def close(self) -> None:
        pass


def _settings_with_all_resource_ids():
    return make_settings(
        metrics_search_resource_id=f"{_SUB}/providers/Microsoft.Search/searchServices/s",
        metrics_postgres_resource_id=f"{_SUB}/providers/Microsoft.DBforPostgreSQL/flexibleServers/p",
        metrics_cosmos_resource_id=f"{_SUB}/providers/Microsoft.DocumentDB/databaseAccounts/c",
        metrics_container_app_resource_id=f"{_SUB}/providers/Microsoft.App/containerApps/a",
    )


async def test_cosmos_panel_queried_with_one_hour_common_time_grain():
    """Regression: the Cosmos panel must use a 1-hour granularity so
    TotalRequestUnits/TotalRequests/ServiceAvailability can be queried
    together, instead of the service's default 5-minute bucket that Azure
    Monitor rejects for that combination."""
    querier = _RecordingQuerier()
    service = ResourceMetricsService(_settings_with_all_resource_ids(), querier=querier)

    report = await service.resources()

    cosmos_panel = next(p for p in report.panels if p.key == "cosmos")
    assert cosmos_panel.status == "ok"
    cosmos_index = [spec.key for spec in PANEL_SPECS].index("cosmos")
    _resource_id, window, granularity = querier.calls[cosmos_index]
    assert granularity == 60
    # Azure Monitor requires the timespan to cover at least one full bucket.
    assert window >= granularity


async def test_non_cosmos_panels_keep_the_default_granularity():
    """Only Cosmos DB needs the coarser grain; every other panel's bucket
    size must be unaffected by the Cosmos-specific override."""
    querier = _RecordingQuerier()
    service = ResourceMetricsService(_settings_with_all_resource_ids(), querier=querier)

    report = await service.resources()

    assert all(p.status == "ok" for p in report.panels)
    assert len(querier.calls) == len(PANEL_SPECS)
    for spec, (_resource_id, _window, granularity) in zip(PANEL_SPECS, querier.calls):
        if spec.key == "cosmos":
            continue
        assert granularity == DEFAULT_GRANULARITY_MINUTES
