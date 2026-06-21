"""ResourceMetricsService: build the admin dashboard's resource panels.

Maps each configured ARM resource id to a small set of platform metrics and reads
them from Azure Monitor. Best-effort by construction: a panel whose id is unset,
whose SDK is missing, or whose query fails is returned as ``unavailable`` with a
short reason — never an error. Panels light up as diagnostics/resource ids
appear.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings
from .azure_monitor import MetricsQuerier
from .models import MetricRequest, ResourceMetricsReport, ResourcePanel

logger = logging.getLogger(__name__)

# How much history each panel summarizes, and the bucket granularity.
DEFAULT_WINDOW_MINUTES = 60
DEFAULT_GRANULARITY_MINUTES = 5


@dataclass(frozen=True)
class _PanelSpec:
    key: str
    display_name: str
    id_attr: str  # Settings attribute holding the ARM resource id
    metrics: tuple[MetricRequest, ...]


# Curated, low-cardinality metric set per resource. Names are the Azure Monitor
# metric ids for each resource type; aggregations match how each reads best.
PANEL_SPECS: tuple[_PanelSpec, ...] = (
    _PanelSpec(
        key="search",
        display_name="Azure AI Search",
        id_attr="metrics_search_resource_id",
        metrics=(
            MetricRequest(name="SearchQueriesPerSecond", label="Queries/sec", aggregation="average"),
            MetricRequest(name="SearchLatency", label="Search latency", aggregation="average", unit="s"),
            MetricRequest(
                name="ThrottledSearchQueriesPercentage",
                label="Throttled %",
                aggregation="average",
                unit="%",
            ),
        ),
    ),
    _PanelSpec(
        key="postgres",
        display_name="PostgreSQL (Flexible Server)",
        id_attr="metrics_postgres_resource_id",
        metrics=(
            MetricRequest(name="cpu_percent", label="CPU %", aggregation="average", unit="%"),
            MetricRequest(name="storage_percent", label="Storage %", aggregation="average", unit="%"),
            MetricRequest(
                name="active_connections", label="Active connections", aggregation="average"
            ),
        ),
    ),
    _PanelSpec(
        key="cosmos",
        display_name="Cosmos DB",
        id_attr="metrics_cosmos_resource_id",
        metrics=(
            MetricRequest(name="TotalRequestUnits", label="Request Units", aggregation="total", unit="RU"),
            MetricRequest(name="TotalRequests", label="Requests", aggregation="total"),
        ),
    ),
    _PanelSpec(
        key="containerApp",
        display_name="Container Apps (API)",
        id_attr="metrics_container_app_resource_id",
        metrics=(
            MetricRequest(name="Replicas", label="Replicas", aggregation="average"),
            MetricRequest(name="RestartCount", label="Restarts", aggregation="total"),
        ),
    ),
)


class ResourceMetricsService:
    def __init__(
        self,
        settings: Settings,
        *,
        querier: MetricsQuerier | None = None,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
        granularity_minutes: int = DEFAULT_GRANULARITY_MINUTES,
    ) -> None:
        self._settings = settings
        self._querier = querier
        self._window = window_minutes
        self._granularity = granularity_minutes
        # Whether we already tried (and failed) to construct the real querier, so
        # we don't repeatedly pay the import/credential cost on every request.
        self._querier_unavailable = False

    async def resources(self) -> ResourceMetricsReport:
        report = ResourceMetricsReport(windowMinutes=self._window)
        if not self._settings.resource_metrics_enabled:
            report.panels = [
                ResourcePanel.unavailable(s.key, s.display_name, "Resource metrics disabled.")
                for s in PANEL_SPECS
            ]
            return report

        for spec in PANEL_SPECS:
            report.panels.append(await self._panel(spec))
        return report

    async def _panel(self, spec: _PanelSpec) -> ResourcePanel:
        resource_id = getattr(self._settings, spec.id_attr, None)
        if not resource_id:
            return ResourcePanel.unavailable(
                spec.key, spec.display_name, "Resource id not configured."
            )
        querier = await self._get_querier()
        if querier is None:
            return ResourcePanel.unavailable(
                spec.key, spec.display_name, "Azure Monitor client unavailable."
            )
        try:
            points = await querier.query(
                resource_id,
                list(spec.metrics),
                window_minutes=self._window,
                granularity_minutes=self._granularity,
            )
        except Exception:  # noqa: BLE001 - resource panels are best-effort
            logger.warning(
                "resource metrics query failed for %s", spec.key, exc_info=True
            )
            return ResourcePanel.unavailable(
                spec.key, spec.display_name, "Metrics query failed."
            )
        return ResourcePanel(
            key=spec.key,
            displayName=spec.display_name,
            status="ok",
            metrics=points,
        )

    async def _get_querier(self) -> MetricsQuerier | None:
        if self._querier is not None:
            return self._querier
        if self._querier_unavailable:
            return None
        try:
            from .azure_monitor import AzureMonitorQuerier

            self._querier = AzureMonitorQuerier()
            return self._querier
        except Exception:  # noqa: BLE001 - SDK/credential absent -> degrade
            logger.warning("Azure Monitor querier construction failed", exc_info=True)
            self._querier_unavailable = True
            return None

    async def close(self) -> None:
        close = getattr(self._querier, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001
                logger.warning("resource metrics querier close failed", exc_info=True)
