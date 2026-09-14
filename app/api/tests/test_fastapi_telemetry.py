"""Real app/distro/instrumentor integration in isolated, no-export processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]


def exercise(case: str) -> dict:
    bootstrap = (
        "import sys; sys.path[:0] = [sys.argv[1], sys.argv[2]]; "
        "from tests.test_fastapi_telemetry import _exercise; _exercise(sys.argv[3])"
    )
    with tempfile.TemporaryDirectory() as home:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", bootstrap, str(ROOT / "api" / "src"),
             str(ROOT / "api"), case],
            cwd=home, capture_output=True, timeout=45,
            env={
                **{key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ},
                "HOME": home, "USERPROFILE": home, "TMP": home, "TEMP": home,
            },
        )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return json.loads(result.stdout)


@pytest.mark.parametrize("case", [
    "gate", "auto_gate", "requests", "errors", "auth", "genai", "sampling", "drop",
])
def test_real_factory_instrumentation(case):
    assert exercise(case)["passed"] is True


def _exercise(case: str) -> None:
    import asyncio
    from contextlib import ExitStack, redirect_stdout
    from importlib.metadata import distribution, version
    import inspect
    import io
    import logging
    import time
    from unittest.mock import patch

    import httpx
    from pydantic_settings.sources import DotEnvSettingsSource
    from opentelemetry import trace
    from opentelemetry.context import Context
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.trace.id_generator import IdGenerator
    from opentelemetry.trace import SpanKind, StatusCode
    import azure.monitor.opentelemetry as monitor
    import azure.monitor.opentelemetry._configure as setup
    from azure.monitor.opentelemetry.exporter import RateLimitedSampler
    from azure.monitor.opentelemetry.exporter._version import VERSION as exporter_version
    from azure.monitor.opentelemetry.exporter.export.trace._exporter import (
        _convert_span_events_to_envelopes, _convert_span_to_envelope,
    )
    from azure.monitor.opentelemetry.exporter.export.trace._utils import _get_DJB2_sample_score
    assert exporter_version == version("azure-monitor-opentelemetry-exporter")
    exporter_source = Path(inspect.getfile(_convert_span_to_envelope)).resolve()
    assert exporter_source == Path(distribution("azure-monitor-opentelemetry-exporter").locate_file(
        "azure/monitor/opentelemetry/exporter/export/trace/_exporter.py",
    )).resolve()

    secret = "PRIVATE-prompt-session-user-key-credential"
    connection = "InstrumentationKey=00000000-0000-0000-0000-000000000001"
    captured = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()
    metric_provider = MeterProvider(metric_readers=[metric_reader])
    metrics.set_meter_provider(metric_provider)
    starts = []
    network = []
    configured = []
    providers = []

    class LowScoreIds(IdGenerator):
        def __init__(self):
            self.next_span = 100
            self.trace_id = int("f149e41eedfaed17bb9f69820803174d", 16)
            assert _get_DJB2_sample_score(f"{self.trace_id:032x}") < 0.0001

        def generate_span_id(self):
            self.next_span += 1
            return self.next_span

        def generate_trace_id(self):
            return self.trace_id

    class CaptureStart(SpanProcessor):
        def on_start(self, span, parent_context=None):
            starts.append({"name": span.name, "attributes": dict(span.attributes or {})})

    provider_class = setup.TracerProvider

    def provider(**kwargs):
        # Only IDs are deterministic. Distro selects the actual shipping sampler;
        # neither the application nor the test replaces it with always-on.
        result = provider_class(**kwargs, id_generator=LowScoreIds())
        result.add_span_processor(CaptureStart())
        providers.append(result)
        return result

    def deny(*_args, **_kwargs):
        network.append(True)
        raise AssertionError("unexpected real transport")

    async def deny_async(*args, **kwargs):
        return deny(*args, **kwargs)

    original_configure = monitor.configure_azure_monitor

    def configure(**kwargs):
        configured.append(kwargs.copy())
        options = {
            name: {"enabled": False}
            for name in ("azure_sdk", "django", "flask", "psycopg2", "requests", "urllib", "urllib3")
        }
        options.update(kwargs.pop("instrumentation_options", {}))
        original_configure(
            **kwargs, instrumentation_options=options,
            enable_live_metrics=False, enable_performance_counters=False,
            resource=Resource({"service.name": "ai4ia-api"}),
        )

    diagnostics = io.StringIO()
    with ExitStack() as stack, redirect_stdout(diagnostics):
        stack.enter_context(patch.dict(os.environ, {
            "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none",
            "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS": "",
            "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST": ".*",
            "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE": ".*",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
            "AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING": "true",
        }))
        stack.enter_context(patch.object(DotEnvSettingsSource, "_read_env_files", return_value={}))
        stack.enter_context(patch.object(httpx.HTTPTransport, "handle_request", deny))
        stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", deny_async))
        stack.enter_context(patch.object(setup, "TracerProvider", provider))
        stack.enter_context(patch.object(setup, "AzureMonitorTraceExporter", lambda **_: captured))
        stack.enter_context(patch.object(monitor, "configure_azure_monitor", configure))
        if case == "auto_gate":
            FastAPIInstrumentor().instrument()
        if case == "drop":
            stack.enter_context(patch.dict(os.environ, {
                "OTEL_TRACES_SAMPLER": "microsoft.rate_limited",
                "OTEL_TRACES_SAMPLER_ARG": "0",
            }))
        from ai4ia_api import main
        from ai4ia_api.genai_telemetry import ATTRIBUTE_NAMES, INSTRUMENTATION_NAME
        from ai4ia_api.request_telemetry import REQUEST_ATTRIBUTES, instrument_app
        from tests.conftest import make_settings
        from fastapi.testclient import TestClient
        from fastapi import HTTPException
        from starlette.responses import StreamingResponse

        assert configured == []  # main's import-time default app is disabled.
        default_app = main.create_app(make_settings())
        assert configured == []
        with TestClient(default_app) as client:
            assert client.get("/health/live").status_code == 200
        assert providers == [] and captured.get_finished_spans() == ()
        if case in ("gate", "auto_gate"):
            settings = make_settings(applicationinsights_connection_string=connection)
            apps = [main.create_app(settings), main.create_app(settings)]
            for app in apps:
                instrument_app(app, connection)
                with TestClient(app) as client:
                    assert client.get("/health/live").status_code == 200
            assert len(configured) == len(providers) == 1, diagnostics.getvalue()
            providers[0].force_flush()
            spans = captured.get_finished_spans()
            assert len(spans) == 2 and all(span.kind == SpanKind.SERVER for span in spans)
            captured.clear()
            disabled = main.create_app(make_settings())
            with TestClient(disabled) as client:
                assert client.get("/health/live").status_code == 200
            providers[0].force_flush()
            assert captured.get_finished_spans() == ()
        else:
            overrides = {}
            if case == "auth":
                from tests.test_auth_entra import API_URI, TENANT, _mint, _new_keypair, _provider
                overrides = {"auth_provider": "entra", "entra_tenant_id": TENANT, "entra_audience": API_URI}
            app = main.create_app(make_settings(
                applicationinsights_connection_string=connection,
                model_gateway_url="https://gateway.invalid",
                **overrides,
            ))
            assert len(configured) == len(providers) == 1, diagnostics.getvalue()
            actual_provider = providers[0]
            assert isinstance(actual_provider.sampler, RateLimitedSampler)
            assert actual_provider.sampler.get_description() == (
                "RateLimitedSampler{0.0}" if case == "drop" else "RateLimitedSampler{5.0}"
            )
            assert not any("sampl" in key for key in configured[0])
            received = []
            request_contexts = []
            gateway_contexts = []

            @app.middleware("http")
            async def observe_parent(request, call_next):
                request_contexts.append(trace.get_current_span().get_span_context())
                return await call_next(request)

            @app.get("/test-error")
            async def fail():
                raise RuntimeError(secret)

            @app.get("/test-auth")
            async def unauthorized():
                raise HTTPException(401, secret, headers={"WWW-Authenticate": "Bearer"})

            @app.get("/test-stream")
            async def streaming():
                async def chunks():
                    received.append(trace.get_current_span().get_span_context())
                    yield b"first\n"
                    await asyncio.sleep(0)
                    received.append(trace.get_current_span().get_span_context())
                    yield b"second\n"
                return StreamingResponse(chunks())

            # Let the default rate window advance rather than changing its limit.
            time.sleep(0.25)
            with TestClient(app, raise_server_exceptions=False) as client:
                shared_http = app.state.gateway._http
                if case in ("genai", "drop"):
                    from ai4ia_api.gateway.client import ModelGatewayClient
                    model = next(item for item in app.state.catalog.models if item.api == "chat" and item.conversational)

                    def reply(request):
                        received.append(json.loads(request.content))
                        gateway_contexts.append(trace.get_current_span().get_span_context())
                        return httpx.Response(200, json={
                            "model": model.id,
                            "choices": [{"message": {"role": "assistant", "content": secret}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                        })

                    gateway_http = httpx.AsyncClient(transport=httpx.MockTransport(reply), trust_env=False)
                    app.state.gateway = ModelGatewayClient(app.state.settings, http_client=gateway_http)
                    session = client.post("/api/sessions", json={
                        "model": model.id, "libraryDocumentIds": [],
                    }).json()
                    response = client.post("/api/chat", json={
                        "sessionId": session["id"], "content": secret, "model": model.id,
                        "stream": False, "allowTools": False, "allowAutomaticMemory": False,
                    }, headers={
                        "baggage": "user=" + secret,
                        "tracestate": "private=" + secret,
                        "traceparent": "00-f149e41eedfaed17bb9f69820803174d-0000000000000001-01",
                    })
                    assert response.status_code == 200
                    assert response.json()["message"]["content"] == secret
                    assert len(received) == 1 and secret in json.dumps(received)
                    assert client.portal is not None
                    client.portal.call(gateway_http.aclose)
                elif case == "errors":
                    response = client.get("/test-error?api-key=" + secret)
                    assert response.status_code == 500
                    response = client.get("/test-auth", headers={"x-correlation-id": secret})
                    assert response.status_code == 401
                    assert response.json()["detail"] == secret
                    assert response.headers["www-authenticate"] == "Bearer"
                    assert response.headers["x-correlation-id"] == secret
                    response = client.get("/test-stream")
                    assert response.text == "first\nsecond\n"
                    assert received and all(context.is_valid for context in received)
                elif case == "auth":
                    key, jwks = _new_keypair("test-key-1")
                    app.state.auth_provider = _provider(jwks, audience=API_URI)
                    assert client.get("/api/models").status_code == 401
                    token = _mint(key, aud=API_URI)
                    assert client.get("/api/models", headers={"Authorization": "Bearer " + token}).status_code == 200
                else:
                    response = client.get("/health/live?api-key=" + secret, headers={
                        "authorization": "Bearer " + secret, "cookie": "session=" + secret,
                        "user-agent": secret, "baggage": "user=" + secret,
                        "x-correlation-id": secret,
                    })
                    assert response.status_code == 200 and response.json() == {"status": "ok"}
                    assert response.headers["x-correlation-id"] == secret
                    response = client.get("/api/sessions/" + secret, headers={"X-Dev-User": secret})
                    assert response.status_code == 404
                    response = client.get("/unknown/" + secret)
                    assert response.status_code == 404
                assert request_contexts and all(context.is_valid for context in request_contexts)
            assert shared_http.is_closed
            actual_provider.force_flush()
            spans = captured.get_finished_spans()
            server = [span for span in spans if span.kind == SpanKind.SERVER]
            if case == "drop":
                assert server == [] and spans == ()
                assert request_contexts and all(context.is_valid for context in request_contexts)
            else:
                assert server and len(server) == len(request_contexts)
            if case == "genai":
                children = [span for span in spans if span.instrumentation_scope.name == INSTRUMENTATION_NAME]
                assert len(children) == 1
                child = children[0]
                parent = next(span for span in server if span.name == "POST /api/chat")
                assert child.parent.span_id == parent.context.span_id
                assert child.context.trace_id == parent.context.trace_id
                assert gateway_contexts[0].span_id == parent.context.span_id
                assert set(child.attributes) <= ATTRIBUTE_NAMES | {"_MS.sampleRate"}
                assert child.attributes["gen_ai.usage.output_tokens"] == 2
                parent_rate = parent.attributes.get("_MS.sampleRate")
                if parent_rate is not None:
                    assert child.attributes.get("_MS.sampleRate") == parent_rate
                # b55 resamples a recorded 100%-rate parent lacking an explicit
                # rate attribute; preserving the SDK is not forcing inheritance.
                assert not child.events and not child.links
                assert secret not in child.to_json()
                assert secret not in json.dumps(_convert_span_to_envelope(child).as_dict())
            for span in server:
                assert set(span.attributes) <= REQUEST_ATTRIBUTES | {"_MS.sampleRate"}
                assert not span.events and not span.links and span.status.description is None
                assert secret not in span.to_json()
                envelope = _convert_span_to_envelope(span)
                assert envelope.data.base_type == "RequestData"
                assert secret not in json.dumps(envelope.as_dict())
                assert _convert_span_events_to_envelopes(span) == []
            if case == "errors":
                assert any(span.status.status_code == StatusCode.ERROR for span in server)
            if case == "sampling":
                # Same exact b55 sampler, both local parent conditions. No
                # application sampling configuration is changed for this proof.
                from opentelemetry.sdk.trace.sampling import Decision
                from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
                from azure.monitor.opentelemetry.exporter._constants import _SAMPLE_RATE_KEY
                dropped_parent = trace.set_span_in_context(NonRecordingSpan(SpanContext(
                    123, 456, is_remote=False, trace_flags=TraceFlags.DEFAULT,
                )), Context())
                denied = actual_provider.sampler.should_sample(
                    dropped_parent, 123, "chat", kind=SpanKind.CLIENT, attributes={},
                )
                assert denied.decision == Decision.DROP
                class RecordedParent(NonRecordingSpan):
                    attributes = {_SAMPLE_RATE_KEY: 20.0}
                    def is_recording(self):
                        return True
                from ai4ia_api.request_telemetry import _RequestSpan
                recorded = _RequestSpan(RecordedParent(SpanContext(
                        123, 456, is_remote=False, trace_flags=TraceFlags.SAMPLED,
                    )), frozenset(), SpanKind.SERVER)
                inheritance_control = RateLimitedSampler(0)
                admitted = inheritance_control.should_sample(
                    trace.set_span_in_context(recorded, Context()), 123, "chat",
                    kind=SpanKind.CLIENT, attributes={},
                )
                assert admitted.decision == Decision.RECORD_AND_SAMPLE
                assert admitted.attributes[_SAMPLE_RATE_KEY] == 20.0
                assert inheritance_control.should_sample(
                    dropped_parent, 123, "chat", kind=SpanKind.CLIENT, attributes={},
                ).decision == Decision.DROP
        assert network == []
        assert secret not in json.dumps(starts)
        assert metric_reader.get_metrics_data() is None
        control_counter = metrics.get_meter("synthetic-control").create_counter("control")
        control_counter.add(1)
        assert metric_reader.get_metrics_data() is not None
        assert all("sampling_ratio" not in args and "sampling_traces_per_second" not in args for args in configured)
        for item in providers:
            item.shutdown()
        metric_provider.shutdown()
        FastAPIInstrumentor().uninstrument()
        logging.disable(logging.CRITICAL)
    print(json.dumps({
        "passed": True,
        "versions": {name: version(name) for name in (
            "azure-monitor-opentelemetry", "azure-monitor-opentelemetry-exporter",
            "opentelemetry-sdk", "opentelemetry-instrumentation-fastapi",
        )},
    }))
