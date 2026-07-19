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

One :meth:`AzureMonitorQuerier.query` call may issue more than one
``query_resources`` request: the batch API requires every metric named in a
single request to support every aggregation requested in that same request, so
a panel whose metrics don't all share one aggregation (e.g. Cosmos DB mixing
Count-only, Total-only, and Average-only metrics) is split into one request
per distinct aggregation and the results are merged back in the caller's
order. Panels whose metrics already share one aggregation still make exactly
one request.

If one aggregation group's request fails (e.g. Azure Monitor rejects that
specific combination), the other groups' already-fetched data is kept rather
than discarded: only the failed group's metrics resolve to a null value
carrying a short, safe ``error`` reason (see :class:`~.models.MetricPoint`),
so the caller can tell a genuine per-metric failure apart from legitimate
no-data-yet and mark the panel ``partial`` instead of silently reporting
``ok`` with holes in it. The SDK's public ``query_resources`` wrapper discards
Azure Monitor's own per-metric ``errorCode``/``errorMessage`` fields before
returning (they only survive on the private, undocumented raw operation), so
the failure signal used here is the HTTP-level exception raised for the whole
group's request instead -- coarser than per-metric, but accurate for every
panel in this module since each aggregation group is at most a couple of
metrics.
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


def _safe_error_reason(exc: Exception) -> str:
    """A short, bounded, non-leaky description of a failed metrics query.

    Only the HTTP status and Azure's short machine error code (e.g.
    ``"BadRequest"``) are used -- never the raw exception text or Azure's
    free-form error message, either of which can echo back the request (the
    resource id, metric namespace, etc.).
    """
    status_code = getattr(exc, "status_code", None)
    error = getattr(exc, "error", None)
    code = getattr(error, "code", None) if error is not None else None
    if status_code and code:
        return f"HTTP {status_code} ({code})"
    if status_code:
        return f"HTTP {status_code}"
    return "query failed"


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
        from azure.core.exceptions import HttpResponseError
        from azure.monitor.querymetrics import MetricAggregationType

        agg_map = {
            "average": MetricAggregationType.AVERAGE,
            "total": MetricAggregationType.TOTAL,
            "maximum": MetricAggregationType.MAXIMUM,
            "count": MetricAggregationType.COUNT,
        }
        namespace = _metric_namespace(resource_id)

        # Production incident: Azure Monitor's batch metrics API rejects a call
        # whose requested aggregation isn't supported by *every* metric named in
        # that same call (e.g. Cosmos DB's TotalRequests supports only Count,
        # TotalRequestUnits only Total/Average/Maximum, and ServiceAvailability
        # only Minimum/Average/Maximum -- no aggregation is common to all
        # three). A single call requesting the union of every panel metric's
        # aggregation is invalid whenever a panel mixes metrics that don't all
        # share one. Group requests by their own aggregation and issue one call
        # per group instead, so each call only ever asks for an aggregation
        # every metric in it actually supports. Panels whose metrics already
        # share one aggregation (the common case) still make exactly one call.
        groups: dict[str, list[MetricRequest]] = {}
        for req in requests:
            groups.setdefault(req.aggregation, []).append(req)

        # Resolve each request against its own group's response only (never a
        # map merged across groups), so two requests that name the same metric
        # under different aggregations can never shadow one another.
        resolved: dict[tuple[str, str], object] = {}
        # Production incident: one aggregation group failing (e.g. an Azure
        # Monitor error specific to that request) used to raise out of this
        # method entirely, discarding data already fetched for other groups in
        # the same panel while the caller silently reported the whole panel
        # "ok" with nulls standing in for the failure. Catch each group's own
        # failure, keep going, and remember a safe reason so every metric in
        # that group resolves to an explicit error instead of an
        # indistinguishable null.
        group_errors: dict[str, str] = {}
        for aggregation, group_requests in groups.items():
            try:
                results = await self._client.query_resources(
                    resource_ids=[resource_id],
                    metric_namespace=namespace,
                    metric_names=[r.name for r in group_requests],
                    timespan=timedelta(minutes=window_minutes),
                    granularity=timedelta(minutes=granularity_minutes),
                    aggregations=[agg_map[aggregation]],
                )
            except HttpResponseError as exc:
                group_errors[aggregation] = _safe_error_reason(exc)
                continue
            metrics = results[0].metrics if results else []
            for metric in metrics:
                resolved[(str(metric.name), aggregation)] = metric

        return [
            self._resolve(
                req,
                resolved.get((req.name, req.aggregation)),
                group_errors.get(req.aggregation),
            )
            for req in requests
        ]

    @staticmethod
    def _resolve(req: MetricRequest, metric, group_error: str | None = None) -> MetricPoint:
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
            error=group_error if value is None else None,
        )

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()
