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

If one aggregation group's request fails outright (e.g. a transport error or
an HTTP-level rejection of the whole call), the other groups' already-fetched
data is kept rather than discarded: only the failed group's metrics resolve
to a null value carrying a short, safe ``error`` reason (see
:class:`~.models.MetricPoint`), so the caller can tell a genuine failure apart
from legitimate no-data-yet and mark the panel ``partial``/``unavailable``
instead of silently reporting ``ok`` with holes in it.

Azure Monitor can also return an overall HTTP 200 where one *specific* metric
within an otherwise-successful group failed its own query (a mismatched
filter, an unsupported dimension, etc.) -- that failure only ever surfaces as
the metric's own ``errorCode``/``errorMessage``, and the SDK's public
``query_resources`` wrapper silently discards both fields while converting
the response into its "friendly" ``MetricsQueryResult``/``Metric`` models.
To recover them without losing that friendly conversion (still used for the
actual values, unchanged), ``query_resources`` is called with a standard
azure-core ``cls`` callback (see
``azure.core.pipeline.policies.CustomHookPolicy`` and the ``ClsType`` pattern
common to every autorest-generated operation): the callback is handed the
same fully-typed, already-deserialized internal
``azure.monitor.querymetrics.models._models.MetricResultsResponse`` the SDK
always builds before its friendly conversion runs, and returns it unchanged
so that conversion proceeds exactly as it does today -- it is a read-only
side channel, not a replacement for the existing value-extraction path.
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


def _safe_metric_error_reason(error_code: str) -> str:
    """A short, bounded, non-leaky description of a single metric's own
    query failure (Azure Monitor's per-metric ``errorCode``, e.g.
    ``"BadRequest"``).

    Mirrors :func:`_safe_error_reason`'s reasoning: only the short machine
    code is surfaced, never ``errorMessage``, which is free-form and can
    echo request details (resource id, filter, etc.) back to the caller.
    """
    return f"metric query failed ({error_code})"


def _safe_metric_error_message(value: object) -> str | None:
    """Normalize and bound Azure's per-metric error message for admin output."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text[:512]


def _extract_metric_errors(
    raw_response: object,
) -> list[tuple[str, str, str | None]]:
    """Pull each metric's own ``errorCode``/``errorMessage`` out of the raw,
    fully-typed ``MetricResultsResponse`` captured via the ``cls`` callback
    in :meth:`AzureMonitorQuerier.query` (see the module docstring).

    Returns ``(metric_name, error_code, bounded_error_message)`` tuples only
    for metrics whose own query failed even though the overall HTTP call
    succeeded. Tolerates a missing/empty/unexpected shape by returning no
    errors rather than raising.
    """
    values = getattr(raw_response, "values_property", None) or []
    if not values:
        return []
    metrics = getattr(values[0], "value", None) or []
    errors: list[tuple[str, str, str | None]] = []
    for metric in metrics:
        code = getattr(metric, "error_code", None)
        if not code or code == "Success":
            continue
        name_obj = getattr(metric, "name", None)
        name = getattr(name_obj, "value", name_obj)
        if not name:
            continue
        errors.append(
            (
                str(name),
                str(code),
                _safe_metric_error_message(getattr(metric, "error_message", None)),
            )
        )
    return errors


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
        # A group can also come back HTTP 200 with one specific metric's own
        # query failed (see the module docstring); captured per-metric via
        # the `cls` callback below, keyed the same way as `resolved` so it
        # never gets confused with a different aggregation of the same name.
        metric_errors: dict[tuple[str, str], tuple[str, str | None]] = {}
        for aggregation, group_requests in groups.items():
            raw_capture: dict[str, object] = {}

            def _capture_raw(_pipeline_response, deserialized, _headers, _sink=raw_capture):
                _sink["value"] = deserialized
                return deserialized

            try:
                results = await self._client.query_resources(
                    resource_ids=[resource_id],
                    metric_namespace=namespace,
                    metric_names=[r.name for r in group_requests],
                    timespan=timedelta(minutes=window_minutes),
                    granularity=timedelta(minutes=granularity_minutes),
                    aggregations=[agg_map[aggregation]],
                    cls=_capture_raw,
                )
            except Exception as exc:  # one transport/service failure must not discard siblings
                group_errors[aggregation] = _safe_error_reason(exc)
                continue
            metrics = results[0].metrics if results else []
            for metric in metrics:
                resolved[(str(metric.name), aggregation)] = metric
            for name, code, message in _extract_metric_errors(
                raw_capture.get("value")
            ):
                metric_errors[(name, aggregation)] = (code, message)

        points: list[MetricPoint] = []
        for req in requests:
            metric_error = metric_errors.get((req.name, req.aggregation))
            group_error = group_errors.get(req.aggregation)
            points.append(
                self._resolve(
                    req,
                    resolved.get((req.name, req.aggregation)),
                    (
                        _safe_metric_error_reason(metric_error[0])
                        if metric_error
                        else group_error
                    ),
                    error_code=metric_error[0] if metric_error else None,
                    error_message=metric_error[1] if metric_error else None,
                )
            )
        return points

    @staticmethod
    def _resolve(
        req: MetricRequest,
        metric,
        error: str | None = None,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> MetricPoint:
        value: float | None = None
        unit = req.unit
        if metric is not None and error is None:
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
            error=error,
            errorCode=error_code,
            errorMessage=error_message,
        )

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()
