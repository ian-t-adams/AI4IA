"""Lightweight structured logging + a correlation-id context.

The correlation id is propagated to the model gateway (``x-correlation-id``)
so a request can be traced across the app, APIM, the proxy, and Foundry.

This module also owns Azure Monitor / OpenTelemetry export (WS3 observability).
``configure_telemetry`` is a strict no-op unless an Application Insights
connection string is supplied, so local/dev runs never start the SDK.
"""
from __future__ import annotations

import contextvars
import logging
import sys
import uuid

# Records logged under this namespace are exported to Application Insights as
# customEvents; ``configure_azure_monitor(logger_name=...)`` scopes log
# collection to it so we do NOT firehose every stdout log line into App Insights
# (Container Apps already ships stdout to Log Analytics — duplicating it here
# would add cost). General ``ai4ia_api.*`` logs live outside this subtree.
TELEMETRY_LOGGER_NAME = "ai4ia_api.telemetry"

_telemetry_logger = logging.getLogger(TELEMETRY_LOGGER_NAME)
_telemetry_configured = False

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str:
    return _correlation_id.get()


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_CorrelationFilter())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s"
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def configure_telemetry(connection_string: str | None) -> bool:
    """Initialize Azure Monitor / OpenTelemetry export (idempotent, best-effort).

    No-op (returns ``False``) when ``connection_string`` is falsy, so local/dev
    with no Application Insights connection string never starts the SDK and
    incurs zero overhead / behaviour change. Safe to call repeatedly; only the
    first successful call configures the exporter. Never raises — telemetry must
    not break startup.
    """
    global _telemetry_configured
    if not connection_string or _telemetry_configured:
        return _telemetry_configured

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(
            connection_string=connection_string,
            logger_name=TELEMETRY_LOGGER_NAME,
        )
    except Exception:  # pragma: no cover - defensive: telemetry never breaks boot
        logging.getLogger(__name__).warning(
            "Azure Monitor telemetry init failed; continuing without export",
            exc_info=True,
        )
        return False

    # The OpenTelemetry log handler is now attached directly to the telemetry
    # logger. Stop its records (the custom usage events) from also propagating to
    # the root stdout handler — they already export to App Insights, and the chat
    # path emits its own structured stdout usage line for Log Analytics.
    _telemetry_logger.propagate = False

    # The distro instruments FastAPI + the Azure SDKs but not httpx, which the
    # chat path uses for outbound model-gateway calls. Instrument it so those
    # surface as App Insights dependencies.
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception:  # pragma: no cover - defensive: optional instrumentation
        logging.getLogger(__name__).warning(
            "httpx telemetry instrumentation failed; outbound calls untraced",
            exc_info=True,
        )

    _telemetry_configured = True
    return True


def telemetry_enabled() -> bool:
    return _telemetry_configured


def emit_custom_event(name: str, attributes: dict[str, object]) -> None:
    """Emit an Application Insights customEvent. No-op unless telemetry is
    configured; best-effort (never raises)."""
    if not _telemetry_configured:
        return
    try:
        extra: dict[str, object] = {"microsoft.custom_event.name": name}
        for key, value in attributes.items():
            if value is not None:
                extra[key] = value
        _telemetry_logger.info(name, extra=extra)
    except Exception:  # pragma: no cover - defensive: telemetry never raises
        pass


def annotate_current_span(correlation_id: str) -> None:
    """Tag the active request span with the correlation id so App Insights
    traces line up with stdout/Log Analytics. No-op unless telemetry is
    configured; best-effort (never raises)."""
    if not _telemetry_configured:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute("correlation_id", correlation_id)
    except Exception:  # pragma: no cover - defensive: telemetry never raises
        pass
