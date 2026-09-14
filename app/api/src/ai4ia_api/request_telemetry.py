"""Instance-bound request instrumentation using the existing OTel pipeline."""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.util.types import Attributes, AttributeValue

from .logging_setup import telemetry_enabled

REQUEST_ATTRIBUTES = frozenset({
    "http.method", "http.request.method", "http.route", "http.scheme", "url.scheme",
    "http.flavor", "network.protocol.version", "http.status_code", "http.response.status_code",
    "error.type",
})
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH"})


def _attribute(key: str, value: AttributeValue, routes: frozenset[str]) -> AttributeValue | None:
    if key == "http.route":
        return value if isinstance(value, str) and value in routes else None
    if key in {"http.method", "http.request.method"}:
        return value if isinstance(value, str) and value in _METHODS else "_OTHER"
    if key in {"http.scheme", "url.scheme"}:
        return value if value in ("http", "https", "ws", "wss") else None
    if key in {"http.flavor", "network.protocol.version"}:
        return value if value in ("1.0", "1.1", "2", "2.0", "3", "3.0") else None
    if key in {"http.status_code", "http.response.status_code"}:
        return value if type(value) is int and 100 <= value <= 599 else None
    if key == "error.type":
        return "server_error" if value else None
    return None


class _RequestSpan(trace.Span):
    """Project writes before they reach the SDK, including explicit exception writes."""

    def __init__(self, span: trace.Span, routes: frozenset[str], kind: trace.SpanKind) -> None:
        self._span = span
        self._routes = routes
        self.kind = kind

    @property
    def attributes(self) -> Mapping[str, AttributeValue]:
        # Azure's sampler reads the parent's public attributes for _MS.sampleRate.
        # The delegated SDK span owns that evidence; do not recreate or reprice it.
        return getattr(self._span, "attributes", {}) or {}

    def end(self, end_time: int | None = None) -> None:
        self._span.end(end_time)

    def get_span_context(self) -> trace.SpanContext:
        return self._span.get_span_context()

    def is_recording(self) -> bool:
        return self._span.is_recording()

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        safe = _attribute(key, value, self._routes)
        if safe is not None:
            self._span.set_attribute(key, safe)

    def set_attributes(self, attributes: Mapping[str, AttributeValue]) -> None:
        for key, value in attributes.items():
            self.set_attribute(key, value)

    def set_status(self, status: trace.Status | trace.StatusCode, description: str | None = None) -> None:
        code = status.status_code if isinstance(status, trace.Status) else status
        self._span.set_status(trace.Status(code))

    def update_name(self, name: str) -> None:
        # The initial name is derived from the registered route and method.
        return

    def add_event(self, name: str, attributes: Attributes = None, timestamp: int | None = None) -> None:
        return

    def add_link(self, context: trace.SpanContext, attributes: Attributes = None) -> None:
        return

    def record_exception(
        self, exception: BaseException, attributes: Attributes = None,
        timestamp: int | None = None, escaped: bool = False,
    ) -> None:
        self._span.set_attribute("error.type", "server_error")
        self._span.set_status(trace.Status(trace.StatusCode.ERROR))


class _RequestTracer(trace.Tracer):
    def __init__(self, tracer: trace.Tracer, routes: frozenset[str]) -> None:
        self._tracer = tracer
        self._routes = routes

    def start_span(
        self, name: str, context: Context | None = None, kind: trace.SpanKind = trace.SpanKind.INTERNAL,
        attributes: Attributes = None, links: Sequence[trace.Link] | None = None,
        start_time: int | None = None, record_exception: bool = True, set_status_on_exception: bool = True,
    ) -> trace.Span:
        safe = {
            key: value for key, raw in (attributes or {}).items()
            if (value := _attribute(key, raw, self._routes)) is not None
        }
        method = safe.get("http.request.method", safe.get("http.method", "HTTP"))
        route = safe.get("http.route")
        safe_name = f"{method} {route}" if route else str(method)
        parent = trace.get_current_span(context).get_span_context()
        if parent.is_valid and parent.is_remote:
            # Keep W3C numeric correlation and flags, not caller-written tracestate
            # values or baggage. The configured sampler still makes the decision.
            context = trace.set_span_in_context(trace.NonRecordingSpan(trace.SpanContext(
                parent.trace_id, parent.span_id, is_remote=True, trace_flags=parent.trace_flags,
            )), Context())
        span = self._tracer.start_span(
            safe_name, context=context, kind=kind, attributes=safe, links=(),
            start_time=start_time, record_exception=False, set_status_on_exception=False,
        )
        return _RequestSpan(span, self._routes, kind)

    @contextmanager
    def start_as_current_span(
        self, name: str, context: Context | None = None, kind: trace.SpanKind = trace.SpanKind.INTERNAL,
        attributes: Attributes = None, links: Sequence[trace.Link] | None = None,
        start_time: int | None = None, record_exception: bool = True,
        set_status_on_exception: bool = True, end_on_exit: bool = True,
    ) -> Iterator[trace.Span]:
        if not attributes:
            # 0.64b0 also wraps BackgroundTask process-wide using the first app's
            # tracer. That hook has no request metadata and must not create
            # spans for a disabled app, or replace its existing context.
            yield trace.get_current_span(context)
            return
        span = self.start_span(name, context, kind, attributes, links, start_time)
        with trace.use_span(
            span, end_on_exit=end_on_exit, record_exception=False, set_status_on_exception=False,
        ):
            yield span


class _RequestProvider(trace.TracerProvider):
    """A public-API facade, not an SDK provider, sampler, processor or exporter."""

    def __init__(self, provider: trace.TracerProvider, routes: frozenset[str]) -> None:
        self._provider = provider
        self._routes = routes

    def get_tracer(
        self, instrumenting_module_name: str, instrumenting_library_version: str | None = None,
        schema_url: str | None = None, attributes: Attributes = None,
    ) -> trace.Tracer:
        return _RequestTracer(self._provider.get_tracer(
            instrumenting_module_name, instrumenting_library_version,
            schema_url=schema_url, attributes=attributes,
        ), self._routes)


def instrument_app(app: FastAPI, connection_string: str | None) -> None:
    if not connection_string or not telemetry_enabled() or getattr(app.state, "request_telemetry", False):
        return
    routes = frozenset(
        route.path for route in iter_route_contexts(app.routes) if isinstance(route.path, str)
    )
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=_RequestProvider(trace.get_tracer_provider(), routes),
        # The instrumentor's metrics bypass the span projection and can retain
        # raw host/path dimensions. Restore request spans, not a new metric feed.
        meter_provider=NoOpMeterProvider(),
        http_capture_headers_server_request=["(?!)"],
        http_capture_headers_server_response=["(?!)"],
        http_capture_headers_sanitize_fields=[".*"],
        exclude_spans=["receive", "send"],
    )
    app.state.request_telemetry = True
