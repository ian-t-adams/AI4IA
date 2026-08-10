"""Lightweight structured logging + a correlation-id context.

The correlation id is propagated to the model gateway (``x-correlation-id``)
so a request can be traced across the app, APIM, the proxy, and Foundry.

This module also owns Azure Monitor / OpenTelemetry export.
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
# Explicit level (not inherited): this logger has no handlers of its own until
# configure_telemetry() attaches one, but ``.info()`` calls in emit_custom_event
# are no-ops whenever the *effective* level filters them out. Effective level is
# inherited from the root logger otherwise, so raising AI4IA_LOG_LEVEL to WARNING
# in production (a normal way to cut stdout noise) would silently drop every
# customEvent -- callers still see success (emit_custom_event never raises) with
# no telemetry ever reaching Application Insights. Pin this logger to INFO so its
# effective level is independent of the root/stdout verbosity setting.
_telemetry_logger.setLevel(logging.INFO)
_telemetry_configured = False

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)

_NOISY_SDK_LOGGERS = (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.cosmos._cosmos_http_logging_policy",
    "azure.monitor.opentelemetry.exporter",
)
_HEALTH_PATHS = frozenset({"/health/live", "/health/ready"})


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


class SuccessfulHealthAccessFilter(logging.Filter):
    """Drop only successful Uvicorn access rows for the two ACA probes."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path = str(args[2]).partition("?")[0]
        raw_status = args[4]
        if not isinstance(raw_status, (int, str)):
            return True
        try:
            status_code = int(raw_status)
        except (TypeError, ValueError):
            return True
        return not (path in _HEALTH_PATHS and 200 <= status_code < 300)


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
    for logger_name in _NOISY_SDK_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    access_logger = logging.getLogger("uvicorn.access")
    for existing in list(access_logger.filters):
        if isinstance(existing, SuccessfulHealthAccessFilter):
            access_logger.removeFilter(existing)
    access_logger.addFilter(SuccessfulHealthAccessFilter())


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


def emit_security_block(category: str, reason: str, source: str) -> None:
    """Emit one bounded, content-free governance denial event."""
    emit_custom_event(
        "security_block",
        {"category": category, "reason": reason, "source": source},
    )


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
