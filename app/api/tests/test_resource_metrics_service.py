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

import pytest

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


@dataclass
class _ScriptedQuerier:
    """Fake ``MetricsQuerier`` that returns caller-scripted points for the
    one panel whose resource id contains ``target_marker`` (matched up by
    metric name) and an all-``ok`` placeholder point for every other panel.
    Lets a single panel's per-metric error state be driven directly, without
    needing a real Azure Monitor/HTTP failure to reach this layer."""

    target_marker: str
    points_by_name: dict[str, MetricPoint]

    async def query(
        self,
        resource_id: str,
        requests: list[MetricRequest],
        *,
        window_minutes: int,
        granularity_minutes: int,
    ) -> list[MetricPoint]:
        if self.target_marker in resource_id:
            return [self.points_by_name[r.name] for r in requests]
        return [
            MetricPoint(name=r.name, label=r.label, aggregation=r.aggregation, value=1.0, unit=r.unit)
            for r in requests
        ]

    async def close(self) -> None:
        pass


async def test_panel_reports_partial_status_when_only_some_metrics_error():
    """Production incident: a metric whose Azure Monitor query genuinely
    failed and a metric with no data yet both resolved to a bare null value,
    so the panel was reported "ok" regardless -- there was no way to tell
    "nothing happened" from "something broke". Pins that a panel with at
    least one (but not all) errored metric is reported "partial", names the
    failing metric in ``detail``, and that the still-resolved metrics'
    values survive unchanged in the panel payload."""
    cosmos_spec = next(s for s in PANEL_SPECS if s.key == "cosmos")
    metrics = list(cosmos_spec.metrics)
    failing = metrics[0]
    points_by_name = {
        m.name: MetricPoint(
            name=m.name,
            label=m.label,
            aggregation=m.aggregation,
            value=None if m.name == failing.name else 1.0,
            error="HTTP 400 (BadRequest)" if m.name == failing.name else None,
        )
        for m in metrics
    }
    querier = _ScriptedQuerier(target_marker="databaseAccounts", points_by_name=points_by_name)
    service = ResourceMetricsService(_settings_with_all_resource_ids(), querier=querier)

    report = await service.resources()

    cosmos_panel = next(p for p in report.panels if p.key == "cosmos")
    assert cosmos_panel.status == "partial"
    assert cosmos_panel.detail
    assert failing.label in cosmos_panel.detail
    by_name = {p.name: p for p in cosmos_panel.metrics}
    for m in metrics:
        if m.name == failing.name:
            assert by_name[m.name].value is None
        else:
            assert by_name[m.name].value == 1.0
    # Other panels are unaffected by Cosmos's per-metric failure.
    assert all(p.status == "ok" for p in report.panels if p.key != "cosmos")


async def test_panel_reports_unavailable_when_every_metric_errors():
    """When every metric requested for a panel failed (every aggregation
    group errored), the panel must be "unavailable" -- not "partial", which
    implies some metrics did resolve -- with a safe, non-empty reason."""
    cosmos_spec = next(s for s in PANEL_SPECS if s.key == "cosmos")
    metrics = list(cosmos_spec.metrics)
    points_by_name = {
        m.name: MetricPoint(
            name=m.name, label=m.label, aggregation=m.aggregation, value=None, error="HTTP 500"
        )
        for m in metrics
    }
    querier = _ScriptedQuerier(target_marker="databaseAccounts", points_by_name=points_by_name)
    service = ResourceMetricsService(_settings_with_all_resource_ids(), querier=querier)

    report = await service.resources()

    cosmos_panel = next(p for p in report.panels if p.key == "cosmos")
    assert cosmos_panel.status == "unavailable"
    assert cosmos_panel.detail


_DEFAULT_ENDPOINT = "https://eastus2.metrics.monitor.azure.com"
_SEARCH_ENDPOINT = "https://eastus.metrics.monitor.azure.com"


class _ManagedQuerier(_RecordingQuerier):
    closed = 0
    fail_close = False

    async def close(self):
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("close failed")


def test_search_metrics_endpoint_reads_explicit_env(monkeypatch):
    monkeypatch.setenv("AI4IA_METRICS_SEARCH_ENDPOINT", _SEARCH_ENDPOINT)
    assert make_settings().metrics_search_endpoint == _SEARCH_ENDPOINT


@pytest.mark.parametrize(
    "search_endpoint",
    [None, "", " ", _DEFAULT_ENDPOINT, _DEFAULT_ENDPOINT + "/", _SEARCH_ENDPOINT],
)
async def test_clients_are_cached_by_resolved_resource_endpoint(monkeypatch, search_endpoint):
    settings = _settings_with_all_resource_ids()
    settings.metrics_endpoint = _DEFAULT_ENDPOINT
    settings.metrics_search_endpoint = search_endpoint
    created: dict[str, _ManagedQuerier] = {}

    def factory(endpoint):
        assert endpoint not in created
        created[endpoint] = _ManagedQuerier()
        return created[endpoint]

    monkeypatch.setattr("ai4ia_api.metrics.azure_monitor.AzureMonitorQuerier", factory)
    service = ResourceMetricsService(settings)
    for _ in range(2):
        assert all(panel.status == "ok" for panel in (await service.resources()).panels)
    expected_search = (search_endpoint or "").strip().rstrip("/") or _DEFAULT_ENDPOINT
    assert set(created) == {expected_search, _DEFAULT_ENDPOINT}
    for endpoint, querier in created.items():
        for resource_id, _, _ in querier.calls:
            expected = expected_search if "/Microsoft.Search/" in resource_id else _DEFAULT_ENDPOINT
            assert endpoint == expected
    assert sum(len(querier.calls) for querier in created.values()) == 6
    await service.close()
    assert all(querier.closed == 1 for querier in created.values())


@pytest.mark.parametrize("failed_endpoint", [_SEARCH_ENDPOINT, _DEFAULT_ENDPOINT])
async def test_client_construction_failure_is_scoped_to_endpoint(monkeypatch, failed_endpoint):
    settings = _settings_with_all_resource_ids()
    settings.metrics_endpoint = _DEFAULT_ENDPOINT
    settings.metrics_search_endpoint = _SEARCH_ENDPOINT
    attempts: list[str] = []

    def factory(endpoint):
        attempts.append(endpoint)
        if endpoint == failed_endpoint:
            raise RuntimeError("client unavailable")
        return _ManagedQuerier()

    monkeypatch.setattr("ai4ia_api.metrics.azure_monitor.AzureMonitorQuerier", factory)
    service = ResourceMetricsService(settings)
    for _ in range(2):
        report = await service.resources()
        for panel in report.panels:
            endpoint = _SEARCH_ENDPOINT if panel.key == "search" else _DEFAULT_ENDPOINT
            assert panel.status == ("unavailable" if endpoint == failed_endpoint else "ok")
    assert sorted(attempts) == sorted([_SEARCH_ENDPOINT, _DEFAULT_ENDPOINT])
    await service.close()


async def test_search_override_works_without_a_default_endpoint(monkeypatch):
    settings = _settings_with_all_resource_ids()
    settings.metrics_search_endpoint = _SEARCH_ENDPOINT
    endpoints = []

    def factory(endpoint):
        endpoints.append(endpoint)
        return _ManagedQuerier()

    monkeypatch.setattr("ai4ia_api.metrics.azure_monitor.AzureMonitorQuerier", factory)
    service = ResourceMetricsService(settings)
    for _ in range(2):
        panels = {panel.key: panel for panel in (await service.resources()).panels}
        assert panels["search"].status == "ok"
        assert panels["cosmos"].status == panels["containerApp"].status == "unavailable"
    assert endpoints == [_SEARCH_ENDPOINT]
    await service.close()


async def test_close_attempts_every_regional_client_after_failure(monkeypatch):
    settings = _settings_with_all_resource_ids()
    settings.metrics_endpoint = _DEFAULT_ENDPOINT
    settings.metrics_search_endpoint = _SEARCH_ENDPOINT
    created = []

    def factory(endpoint):
        querier = _ManagedQuerier()
        querier.fail_close = endpoint == _SEARCH_ENDPOINT
        created.append(querier)
        return querier

    monkeypatch.setattr("ai4ia_api.metrics.azure_monitor.AzureMonitorQuerier", factory)
    service = ResourceMetricsService(settings)
    await service.resources()
    await service.close()
    assert len(created) == 2
    assert all(querier.closed == 1 for querier in created)


async def test_injected_querier_serves_all_regions_and_closes_once(monkeypatch):
    settings = _settings_with_all_resource_ids()
    settings.metrics_endpoint = _DEFAULT_ENDPOINT
    settings.metrics_search_endpoint = _SEARCH_ENDPOINT

    def unexpected_factory(endpoint):
        raise AssertionError("An injected querier must not construct an SDK client.")

    monkeypatch.setattr("ai4ia_api.metrics.azure_monitor.AzureMonitorQuerier", unexpected_factory)
    querier = _ManagedQuerier()
    service = ResourceMetricsService(settings, querier=querier)
    assert all(panel.status == "ok" for panel in (await service.resources()).panels)
    assert len(querier.calls) == 3
    await service.close()
    assert querier.closed == 1
