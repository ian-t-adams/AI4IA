"""Azure Monitor metrics querier (lazy SDK import).

Wraps ``azure.monitor.query.aio.MetricsQueryClient`` reached via the api managed
identity (RBAC: Monitoring Reader). The SDK is imported lazily inside ``__init__``
so the app and tests run without ``azure-monitor-query`` installed and the service
can degrade gracefully. A small in-process protocol (:class:`MetricsQuerier`) lets
tests inject a fake querier with zero Azure dependency.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Protocol, runtime_checkable

from .models import MetricPoint, MetricRequest


@runtime_checkable
class MetricsQuerier(Protocol):
    async def query(
        self,
        resource_id: str,
        requests: list[MetricRequest],
        *,
        window_minutes: int,
        granularity_minutes: int,
    ) -> list[MetricPoint]: ...

    async def close(self) -> None: ...


class AzureMonitorQuerier:
    """Concrete querier over Azure Monitor's metrics data plane."""

    def __init__(self) -> None:
        from azure.identity.aio import DefaultAzureCredential
        from azure.monitor.query.aio import MetricsQueryClient  # pyright: ignore[reportAttributeAccessIssue]

        self._credential = DefaultAzureCredential()
        self._client = MetricsQueryClient(self._credential)

    async def query(
        self,
        resource_id: str,
        requests: list[MetricRequest],
        *,
        window_minutes: int,
        granularity_minutes: int,
    ) -> list[MetricPoint]:
        from azure.monitor.query import MetricAggregationType  # pyright: ignore[reportAttributeAccessIssue]

        # Request every aggregation the panel needs once; read the configured one
        # off each metric's latest non-null datapoint below.
        agg_map = {
            "average": MetricAggregationType.AVERAGE,
            "total": MetricAggregationType.TOTAL,
            "maximum": MetricAggregationType.MAXIMUM,
        }
        aggregations = sorted({r.aggregation for r in requests})
        response = await self._client.query_resource(
            resource_id,
            metric_names=[r.name for r in requests],
            timespan=timedelta(minutes=window_minutes),
            granularity=timedelta(minutes=granularity_minutes),
            aggregations=[agg_map[a] for a in aggregations],
        )
        by_name = {m.name: m for m in response.metrics}
        points: list[MetricPoint] = []
        for req in requests:
            points.append(self._resolve(req, by_name.get(req.name)))
        return points

    @staticmethod
    def _resolve(req: MetricRequest, metric) -> MetricPoint:
        value: float | None = None
        unit = req.unit
        if metric is not None:
            if getattr(metric, "unit", None):
                unit = str(metric.unit)
            series = getattr(metric, "timeseries", None) or []
            # Walk datapoints newest-last; take the most recent that carries the
            # requested aggregation (Azure leaves trailing buckets null).
            for ts in series:
                for dp in reversed(getattr(ts, "data", None) or []):
                    candidate = getattr(dp, req.aggregation, None)
                    if candidate is not None:
                        value = float(candidate)
                        break
                if value is not None:
                    break
        return MetricPoint(
            name=req.name,
            label=req.label,
            aggregation=req.aggregation,
            value=value,
            unit=unit,
        )

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()
