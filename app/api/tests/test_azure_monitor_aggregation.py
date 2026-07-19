"""Regression coverage for AzureMonitorQuerier's per-aggregation call grouping.

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

Second, related production incident: fixing the granularity above surfaced
that the same call was still broken by a second, independent cause. Azure
Monitor's batch metrics API requires every metric named in *one* call to
support every aggregation requested in that same call. The Cosmos DB panel
combines ``TotalRequestUnits`` (Total/Average/Maximum only),
``TotalRequests`` (Count only), and ``ServiceAvailability``
(Minimum/Average/Maximum only) -- no aggregation is common to all three, so
one call requesting the union ``{average, count, total}`` for all three names
is rejected outright regardless of granularity. These tests pin that
``AzureMonitorQuerier`` splits a mixed-aggregation panel into one
``query_resources`` call per aggregation (asserting the exact calls made),
merges the results back in the caller's original order without duplicating or
dropping any metric, and still makes exactly one call for panels whose
metrics already share a single aggregation (the common case, unchanged).
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


@dataclass
class _PerAggregationRecordingClient:
    """Stands in for ``MetricsClient.query_resources`` when a test needs a
    *different* canned result per aggregation -- i.e. when
    ``AzureMonitorQuerier.query`` is expected to split one panel into
    multiple calls. Keyed by the SDK's ``MetricAggregationType`` requested;
    records every call made (in order) so tests can assert the exact
    ``metric_names``/``aggregations`` sent per call."""

    results_by_aggregation: dict[Any, list[_FakeResourceResult]]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def query_resources(self, **kwargs: Any):
        self.calls.append(kwargs)
        (aggregation,) = kwargs["aggregations"]
        return self.results_by_aggregation.get(aggregation, [])

    async def close(self) -> None:
        pass


def _querier_with(client: _RecordingClient | _PerAggregationRecordingClient) -> AzureMonitorQuerier:
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


async def test_cosmos_panel_mixed_aggregations_split_into_one_call_per_aggregation():
    """Production incident: the Cosmos DB panel's three metrics need three
    different, non-overlapping aggregations --
    ``TotalRequestUnits``=Total, ``TotalRequests``=Count,
    ``ServiceAvailability``=Average -- and no aggregation is common to all
    three. ``AzureMonitorQuerier`` must issue one ``query_resources`` call
    per aggregation (never a single call requesting the union), and merge
    the three calls' results back in the caller's original request order
    with no duplicates."""
    from azure.monitor.querymetrics import MetricAggregationType

    cosmos_spec = next(s for s in PANEL_SPECS if s.key == "cosmos")
    requests = list(cosmos_spec.metrics)
    # Pin the fixture to the real panel spec so this test tracks it, not a
    # hand-rolled stand-in that could silently drift from production.
    assert {r.aggregation for r in requests} == {"total", "count", "average"}

    results_by_aggregation = {
        MetricAggregationType.TOTAL: [
            _FakeResourceResult(
                metrics=[
                    _FakeMetric(
                        name="TotalRequestUnits",
                        timeseries=[_FakeTimeSeries(data=[_FakeDataPoint(total=123.0)])],
                    )
                ]
            )
        ],
        MetricAggregationType.COUNT: [
            _FakeResourceResult(
                metrics=[
                    _FakeMetric(
                        name="TotalRequests",
                        timeseries=[_FakeTimeSeries(data=[_FakeDataPoint(count=456.0)])],
                    )
                ]
            )
        ],
        MetricAggregationType.AVERAGE: [
            _FakeResourceResult(
                metrics=[
                    _FakeMetric(
                        name="ServiceAvailability",
                        timeseries=[_FakeTimeSeries(data=[_FakeDataPoint(average=99.9)])],
                    )
                ]
            )
        ],
    }
    client = _PerAggregationRecordingClient(results_by_aggregation=results_by_aggregation)
    querier = _querier_with(client)

    points = await querier.query(
        "/subscriptions/x/resourceGroups/rg/providers/Microsoft.DocumentDB/databaseAccounts/c",
        requests,
        window_minutes=60,
        granularity_minutes=60,
    )

    # Exactly one call per distinct aggregation -- never one combined call
    # requesting the union of all three.
    assert len(client.calls) == 3
    for call in client.calls:
        assert len(call["aggregations"]) == 1
        assert call["granularity"] == timedelta(minutes=60)
    calls_by_aggregation = {call["aggregations"][0]: call for call in client.calls}
    assert set(calls_by_aggregation) == {
        MetricAggregationType.TOTAL,
        MetricAggregationType.COUNT,
        MetricAggregationType.AVERAGE,
    }
    assert calls_by_aggregation[MetricAggregationType.TOTAL]["metric_names"] == ["TotalRequestUnits"]
    assert calls_by_aggregation[MetricAggregationType.COUNT]["metric_names"] == ["TotalRequests"]
    assert calls_by_aggregation[MetricAggregationType.AVERAGE]["metric_names"] == ["ServiceAvailability"]

    # Results merge back in the caller's original request order, one point
    # per request, with no duplicates.
    assert [p.name for p in points] == [r.name for r in requests]
    by_name = {p.name: p for p in points}
    assert by_name["TotalRequestUnits"].value == 123.0
    assert by_name["TotalRequests"].value == 456.0
    assert by_name["ServiceAvailability"].value == 99.9


async def test_container_app_panel_two_aggregations_split_into_two_calls():
    """Same class of incompatibility, a different panel: Container Apps
    mixes ``total`` (Requests, RestartCount) and ``average`` (ResponseTime,
    Replicas). Confirms the per-aggregation split generalizes to a group
    with more than one metric per aggregation, not just Cosmos's
    one-metric-per-group case."""
    from azure.monitor.querymetrics import MetricAggregationType

    container_spec = next(s for s in PANEL_SPECS if s.key == "containerApp")
    requests = list(container_spec.metrics)
    assert {r.aggregation for r in requests} == {"total", "average"}

    results_by_aggregation = {
        MetricAggregationType.TOTAL: [
            _FakeResourceResult(
                metrics=[
                    _FakeMetric(
                        name="Requests",
                        timeseries=[_FakeTimeSeries(data=[_FakeDataPoint(total=10.0)])],
                    ),
                    _FakeMetric(
                        name="RestartCount",
                        timeseries=[_FakeTimeSeries(data=[_FakeDataPoint(total=2.0)])],
                    ),
                ]
            )
        ],
        MetricAggregationType.AVERAGE: [
            _FakeResourceResult(
                metrics=[
                    _FakeMetric(
                        name="ResponseTime",
                        timeseries=[_FakeTimeSeries(data=[_FakeDataPoint(average=120.0)])],
                    ),
                    _FakeMetric(
                        name="Replicas",
                        timeseries=[_FakeTimeSeries(data=[_FakeDataPoint(average=3.0)])],
                    ),
                ]
            )
        ],
    }
    client = _PerAggregationRecordingClient(results_by_aggregation=results_by_aggregation)
    querier = _querier_with(client)

    points = await querier.query(
        "/subscriptions/x/resourceGroups/rg/providers/Microsoft.App/containerApps/a",
        requests,
        window_minutes=60,
        granularity_minutes=5,
    )

    assert len(client.calls) == 2
    for call in client.calls:
        assert len(call["aggregations"]) == 1
    calls_by_aggregation = {call["aggregations"][0]: call for call in client.calls}
    assert set(calls_by_aggregation[MetricAggregationType.TOTAL]["metric_names"]) == {
        "Requests",
        "RestartCount",
    }
    assert set(calls_by_aggregation[MetricAggregationType.AVERAGE]["metric_names"]) == {
        "ResponseTime",
        "Replicas",
    }

    assert [p.name for p in points] == [r.name for r in requests]
    by_name = {p.name: p for p in points}
    assert by_name["Requests"].value == 10.0
    assert by_name["RestartCount"].value == 2.0
    assert by_name["ResponseTime"].value == 120.0
    assert by_name["Replicas"].value == 3.0


async def test_homogeneous_aggregation_panel_still_makes_one_call():
    """A panel whose metrics all share one aggregation (e.g. Azure AI
    Search's Queries/sec, Search latency, and Throttled % -- all
    ``average``) must still make exactly one ``query_resources`` call, not
    one per metric: the per-aggregation split must not change behavior for
    the common, already-working case."""
    search_spec = next(s for s in PANEL_SPECS if s.key == "search")
    requests = list(search_spec.metrics)
    assert {r.aggregation for r in requests} == {"average"}

    fake_result = [
        _FakeResourceResult(
            metrics=[
                _FakeMetric(
                    name=r.name,
                    timeseries=[_FakeTimeSeries(data=[_FakeDataPoint(average=1.0)])],
                )
                for r in requests
            ]
        )
    ]
    client = _RecordingClient(result=fake_result)
    querier = _querier_with(client)

    points = await querier.query(
        "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Search/searchServices/s",
        requests,
        window_minutes=60,
        granularity_minutes=5,
    )

    assert len(client.calls) == 1
    assert len(client.calls[0]["metric_names"]) == len(requests)
    assert len(points) == len(requests)
