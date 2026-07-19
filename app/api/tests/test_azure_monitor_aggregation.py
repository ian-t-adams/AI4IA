"""Regression coverage for AzureMonitorQuerier's Count-aggregation support.

Production incident: the admin resource-metrics dashboard's Cosmos DB panel
requested ``TotalRequests`` with the ``Total`` aggregation. Azure Monitor's
Cosmos DB metric definitions only support the ``Count`` aggregation for
``TotalRequests`` (see the "Microsoft.DocumentDB/databaseAccounts" entry in
Azure Monitor's supported-metrics reference) -- requesting ``Total`` for that
metric is rejected. These tests pin that ``"count"`` is a valid
:class:`~ai4ia_api.metrics.models.MetricRequest` aggregation, that
``AzureMonitorQuerier`` maps it to the SDK's ``MetricAggregationType.COUNT``
without raising ``KeyError``, and that a resolved datapoint's ``count`` field
is read back correctly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from ai4ia_api.metrics.azure_monitor import AzureMonitorQuerier
from ai4ia_api.metrics.models import MetricRequest
from ai4ia_api.metrics.service import PANEL_SPECS


@dataclass
class _FakeDataPoint:
    count: float | None = None
    total: float | None = None
    average: float | None = None
    maximum: float | None = None


@dataclass
class _FakeTimeSeries:
    data: list[_FakeDataPoint]


@dataclass
class _FakeMetric:
    name: str
    timeseries: list[_FakeTimeSeries]
    unit: str | None = None


@dataclass
class _FakeResourceResult:
    metrics: list[_FakeMetric]


@dataclass
class _RecordingClient:
    """Stands in for ``MetricsClient.query_resources``: records the
    aggregations it was asked for and returns a canned result, so the test
    never makes a real network/credential call."""

    result: list[_FakeResourceResult]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def query_resources(self, **kwargs: Any):
        self.calls.append(kwargs)
        return self.result

    async def close(self) -> None:
        pass


def _querier_with(client: _RecordingClient) -> AzureMonitorQuerier:
    """Build a real ``AzureMonitorQuerier`` (no network calls at
    construction time -- credential/client creation is lazy) and swap in a
    fake client so ``.query()`` never leaves the process."""
    q = AzureMonitorQuerier("https://fake.metrics.monitor.azure.com")
    q._client = client  # type: ignore[assignment]
    return q


async def test_count_aggregation_maps_without_keyerror_and_resolves_value():
    """A ``MetricRequest`` with ``aggregation="count"`` must not raise
    ``KeyError`` building the SDK aggregation list, and the resolved point's
    value must come from the datapoint's ``count`` field."""
    from azure.monitor.querymetrics import MetricAggregationType

    fake_result = [
        _FakeResourceResult(
            metrics=[
                _FakeMetric(
                    name="TotalRequests",
                    timeseries=[_FakeTimeSeries(data=[_FakeDataPoint(count=42.0)])],
                )
            ]
        )
    ]
    client = _RecordingClient(result=fake_result)
    querier = _querier_with(client)

    points = await querier.query(
        "/subscriptions/x/resourceGroups/rg/providers/Microsoft.DocumentDB/databaseAccounts/c",
        [MetricRequest(name="TotalRequests", label="Requests", aggregation="count")],
        window_minutes=60,
        granularity_minutes=60,
    )

    assert len(points) == 1
    assert points[0].aggregation == "count"
    assert points[0].value == 42.0
    # The SDK must have been asked for the Count aggregation, not Total.
    (call,) = client.calls
    assert call["aggregations"] == [MetricAggregationType.COUNT]
    assert call["granularity"] == timedelta(minutes=60)


async def test_cosmos_panel_spec_requests_count_for_total_requests():
    """Regression: the Cosmos panel spec must request ``TotalRequests`` with
    the ``count`` aggregation (Azure Monitor rejects any other aggregation
    for that metric name on Cosmos DB), not ``total``."""
    cosmos_spec = next(s for s in PANEL_SPECS if s.key == "cosmos")
    total_requests = next(m for m in cosmos_spec.metrics if m.name == "TotalRequests")
    assert total_requests.aggregation == "count"
