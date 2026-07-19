"""Azure Monitor metrics querier (lazy SDK import).

Wraps ``azure.monitor.querymetrics.aio.MetricsClient`` — the Azure Monitor metrics
BATCH data-plane (``metrics:getBatch``) — reached via the api managed identity. The
batch API requires **subscription-scope** Monitoring Reader on that identity
(per-resource Monitoring Reader is not sufficient); that grant lives in
``infra/main.bicep``. The client targets a REGIONAL endpoint
(``https://<region>.metrics.monitor.azure.com``) supplied by the caller and the
per-request metric namespace is derived from the ARM resource id. The SDK is
imported lazily inside ``__init__`` so the app and tests run without
``azure-monitor-querymetrics`` installed and the service can degrade gracefully. A
small in-process protocol (:class:`MetricsQuerier`) lets tests inject a fake querier
with zero Azure dependency.
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


def _metric_namespace(resource_id: str) -> str:
    """Derive the Azure Monitor metric namespace from an ARM resource id.

    The namespace is the ``<Provider>/<type>`` pair immediately after
    ``/providers/`` — e.g.
    ``/subscriptions/…/providers/Microsoft.DocumentDB/databaseAccounts/<name>``
    -> ``Microsoft.DocumentDB/databaseAccounts``. Raises ``ValueError`` when the id
    carries no ``/providers/`` segment or too few trailing segments to parse.
    """
    marker = "/providers/"
    index = resource_id.find(marker)
    if index == -1:
        raise ValueError(f"resource id has no '/providers/' segment: {resource_id!r}")
    parts = [p for p in resource_id[index + len(marker):].split("/") if p]
    if len(parts) < 2:
        raise ValueError(
            f"cannot derive metric namespace from resource id: {resource_id!r}"
        )
    return f"{parts[0]}/{parts[1]}"


class AzureMonitorQuerier:
    """Concrete querier over Azure Monitor's batch metrics data plane."""

    def __init__(self, endpoint: str) -> None:
        if not endpoint:
            raise ValueError("Azure Monitor metrics endpoint is required")
        from azure.identity.aio import DefaultAzureCredential
        from azure.monitor.querymetrics.aio import MetricsClient

        self._credential = DefaultAzureCredential()
        self._client = MetricsClient(endpoint, self._credential)

    async def query(
        self,
        resource_id: str,
        requests: list[MetricRequest],
        *,
        window_minutes: int,
        granularity_minutes: int,
    ) -> list[MetricPoint]:
        from azure.monitor.querymetrics import MetricAggregationType

        # Request every aggregation the panel needs once; read the configured one
        # off each metric's latest non-null datapoint below.
        agg_map = {
            "average": MetricAggregationType.AVERAGE,
            "total": MetricAggregationType.TOTAL,
            "maximum": MetricAggregationType.MAXIMUM,
            "count": MetricAggregationType.COUNT,
        }
        namespace = _metric_namespace(resource_id)
        aggregations = sorted({r.aggregation for r in requests})
        # Batch data-plane API: one resource per call, namespace derived from its id.
        results = await self._client.query_resources(
            resource_ids=[resource_id],
            metric_namespace=namespace,
            metric_names=[r.name for r in requests],
            timespan=timedelta(minutes=window_minutes),
            granularity=timedelta(minutes=granularity_minutes),
            aggregations=[agg_map[a] for a in aggregations],
        )
        metrics = results[0].metrics if results else []
        by_name = {str(m.name): m for m in metrics}
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
