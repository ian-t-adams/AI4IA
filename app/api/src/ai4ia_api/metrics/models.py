"""Response + spec models for the admin resource-metrics panels."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

PanelStatus = Literal["ok", "unavailable"]
Aggregation = Literal["average", "total", "maximum"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MetricRequest(BaseModel):
    """A single metric to read from a resource and how to aggregate it."""

    name: str  # Azure Monitor metric name, e.g. "cpu_percent"
    label: str  # human label for the dashboard, e.g. "CPU %"
    aggregation: Aggregation = "average"
    unit: str | None = None


class MetricPoint(BaseModel):
    """A resolved metric value (or a null value when no data was returned)."""

    name: str
    label: str
    aggregation: Aggregation
    value: float | None = None
    unit: str | None = None


class ResourcePanel(BaseModel):
    key: str  # "search" | "postgres" | "cosmos" | "containerApp"
    displayName: str
    status: PanelStatus = "unavailable"
    # Why a panel is unavailable (not configured / SDK absent / query failed / disabled).
    detail: str | None = None
    metrics: list[MetricPoint] = Field(default_factory=list)

    @classmethod
    def unavailable(cls, key: str, display_name: str, detail: str) -> "ResourcePanel":
        return cls(key=key, displayName=display_name, status="unavailable", detail=detail)


class ResourceMetricsReport(BaseModel):
    generatedAt: datetime = Field(default_factory=_now)
    windowMinutes: int
    panels: list[ResourcePanel] = Field(default_factory=list)

    @property
    def anyAvailable(self) -> bool:
        return any(p.status == "ok" for p in self.panels)
