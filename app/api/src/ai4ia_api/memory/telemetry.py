"""Content-free, low-cardinality memory operation telemetry."""
from __future__ import annotations

import time

from ..logging_setup import emit_custom_event


def emit_memory_operation(
    operation: str,
    status: str,
    source: str,
    started: float,
    *,
    count: int | None = None,
) -> None:
    emit_custom_event(
        "memory_operation",
        {
            "operation": operation,
            "status": status,
            "source": source,
            "count": count,
            "latencyMs": int((time.monotonic() - started) * 1000),
        },
    )
