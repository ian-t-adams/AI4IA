"""Telemetry (Azure Monitor / OpenTelemetry) wiring.

Covers the observability contract: export is a strict no-op without an
Application Insights connection string (local/dev unaffected), and the chat
path emits a ``chat_completion`` customEvent best-effort (never raising).
"""
from __future__ import annotations

import logging

import pytest

from ai4ia_api import logging_setup
from ai4ia_api.catalog import DeploymentOption
from ai4ia_api.usage.memory_repo import InMemoryUsageRepository
from ai4ia_api.usage.models import TokenUsage, UsageTarget
from ai4ia_api.usage.pricing import PriceRate, PricingBook
from ai4ia_api.usage.service import UsageService


def _service(repo=None) -> UsageService:
    pricing = PricingBook(
        {"gpt-x": PriceRate(input_per_1m=2.0, output_per_1m=8.0)},
        currency="USD",
        version="p-1",
    )
    return UsageService(repo or InMemoryUsageRepository(), pricing, enabled=True)


def _deployment() -> DeploymentOption:
    return DeploymentOption(
        region="eastus2", dataZone=None, sku="GlobalStandard", deploymentName="gpt-x-dep"
    )


def _known_usage() -> TokenUsage:
    return TokenUsage(prompt=1000, completion=500, total=1500, known=True, complete=True, calls=1)


def test_configure_telemetry_noop_without_connection_string(monkeypatch):
    # Force a clean starting state; monkeypatch restores it after the test.
    monkeypatch.setattr(logging_setup, "_telemetry_configured", False)
    assert logging_setup.configure_telemetry(None) is False
    assert logging_setup.configure_telemetry("") is False
    assert logging_setup._telemetry_configured is False


def test_emit_custom_event_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(logging_setup, "_telemetry_configured", False)
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logging_setup._telemetry_logger.addHandler(handler)
    try:
        # Must not raise and must emit nothing while telemetry is off.
        logging_setup.emit_custom_event("chat_completion", {"model": "gpt-x"})
    finally:
        logging_setup._telemetry_logger.removeHandler(handler)
    assert records == []


def test_telemetry_logger_stays_at_info_when_root_level_is_raised(monkeypatch):
    """Regression for MEDIUM-1: the telemetry logger had no explicit level of
    its own, so its *effective* level was inherited from the root logger.
    Raising ``AI4IA_LOG_LEVEL`` to WARNING in production -- a normal way to
    cut stdout noise -- silently dropped every customEvent: callers still saw
    success (``emit_custom_event`` never raises) while nothing reached
    Application Insights. Drives the real ``configure_logging`` entry point
    (the same one ``main.py`` calls with ``settings.log_level``) with the
    root logger genuinely at WARNING, then exercises the real logger with a
    capture handler (no mock) so a future regression that removes the
    explicit ``setLevel`` can't hide behind one."""
    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    records: list[logging.LogRecord] = []
    try:
        logging_setup.configure_logging("WARNING")
        assert root.level == logging.WARNING

        monkeypatch.setattr(logging_setup, "_telemetry_configured", True)

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        logging_setup._telemetry_logger.addHandler(handler)
        try:
            # The crux of the fix: effective level must stay INFO even
            # though root is now WARNING.
            assert logging_setup._telemetry_logger.isEnabledFor(logging.INFO)
            logging_setup.emit_custom_event("chat_completion", {"model": "gpt-x"})
        finally:
            logging_setup._telemetry_logger.removeHandler(handler)
    finally:
        root.setLevel(original_level)
        root.handlers.clear()
        root.handlers.extend(original_handlers)

    assert len(records) == 1
    assert records[0].model == "gpt-x"


def test_annotate_current_span_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(logging_setup, "_telemetry_configured", False)
    # No active span / no SDK: must be a silent no-op.
    logging_setup.annotate_current_span("cid-123")


def test_emit_custom_event_logs_with_event_name_when_enabled(monkeypatch):
    monkeypatch.setattr(logging_setup, "_telemetry_configured", True)
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logging_setup._telemetry_logger.addHandler(handler)
    try:
        logging_setup.emit_custom_event(
            "chat_completion", {"model": "gpt-x", "totalTokens": 1500, "skip": None}
        )
    finally:
        logging_setup._telemetry_logger.removeHandler(handler)

    assert len(records) == 1
    rec = records[0]
    # The customEvent name marker drives App Insights' customEvents table mapping.
    assert getattr(rec, "microsoft.custom_event.name") == "chat_completion"
    assert rec.model == "gpt-x"
    assert rec.totalTokens == 1500
    # None-valued dimensions are dropped (App Insights dimensions can't be null).
    assert not hasattr(rec, "skip")


def test_configure_telemetry_initializes_once(monkeypatch):
    import azure.monitor.opentelemetry as amo
    import opentelemetry.instrumentation.httpx as otel_httpx

    monkeypatch.setattr(logging_setup, "_telemetry_configured", False)
    monkeypatch.setattr(logging_setup._telemetry_logger, "propagate", True)

    calls: dict[str, int] = {"configure": 0, "instrument": 0}

    def _fake_configure(**kwargs):
        calls["configure"] += 1
        assert kwargs["connection_string"] == "InstrumentationKey=abc"
        assert kwargs["logger_name"] == logging_setup.TELEMETRY_LOGGER_NAME

    class _FakeInstrumentor:
        def instrument(self, *a, **k):
            calls["instrument"] += 1

    monkeypatch.setattr(amo, "configure_azure_monitor", _fake_configure)
    monkeypatch.setattr(otel_httpx, "HTTPXClientInstrumentor", _FakeInstrumentor)

    assert logging_setup.configure_telemetry("InstrumentationKey=abc") is True
    assert logging_setup._telemetry_configured is True
    # Custom-event records are kept out of the root stdout handler once exported.
    assert logging_setup._telemetry_logger.propagate is False
    # Idempotent: a second call must not reconfigure the exporter.
    assert logging_setup.configure_telemetry("InstrumentationKey=abc") is True
    assert calls == {"configure": 1, "instrument": 1}


def test_configure_telemetry_swallows_init_failure(monkeypatch):
    import azure.monitor.opentelemetry as amo

    monkeypatch.setattr(logging_setup, "_telemetry_configured", False)

    def _boom(**kwargs):
        raise RuntimeError("exporter down")

    monkeypatch.setattr(amo, "configure_azure_monitor", _boom)
    # Telemetry init failure must never break startup.
    assert logging_setup.configure_telemetry("InstrumentationKey=abc") is False
    assert logging_setup._telemetry_configured is False


async def test_record_completion_emits_chat_completion_event(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.usage.service.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    svc = _service()
    await svc.record_completion(
        user_id="u1",
        session_id="s1",
        model_id="gpt-x",
        deployment=_deployment(),
        usage=_known_usage(),
        agent="research",
        correlation_id="cid-9",
    )
    assert len(captured) == 1
    name, attrs = captured[0]
    assert name == "chat_completion"
    assert attrs["provider"] == "azure_openai"
    assert attrs["model"] == "gpt-x"
    assert attrs["deployment"] == "gpt-x-dep"
    assert attrs["target"] == "gpt-x-dep"
    assert attrs["agent"] == "research"
    assert attrs["promptTokens"] == 1000
    assert attrs["completionTokens"] == 500
    assert attrs["totalTokens"] == 1500
    assert attrs["status"] == "complete"
    assert attrs["billable"] is True
    assert attrs["estCostUsd"] == pytest.approx(0.006)
    assert attrs["correlationId"] == "cid-9"


async def test_record_completion_emits_provider_and_managed_target(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.usage.service.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    svc = _service()
    await svc.record_completion(
        user_id="u1",
        session_id="s1",
        model_id="gpt-realtime",
        target=UsageTarget.managed_service(
            provider="speech_voice_live", target="managed_voice_live", region="eastus2"
        ),
        usage=TokenUsage(known=False, complete=False, calls=1),
        correlation_id="cid-10",
    )
    assert len(captured) == 1
    _, attrs = captured[0]
    assert attrs["provider"] == "speech_voice_live"
    assert attrs["deployment"] is None
    assert attrs["target"] == "managed_voice_live"
    assert attrs["billable"] is False
    assert attrs["usageKnown"] is False
    assert attrs["correlationId"] == "cid-10"


async def test_record_completion_survives_event_emit_failure(monkeypatch):
    def _boom(name, attrs):
        raise RuntimeError("event sink down")

    monkeypatch.setattr("ai4ia_api.usage.service.emit_custom_event", _boom)
    repo = InMemoryUsageRepository()
    svc = _service(repo=repo)
    # A failing customEvent emit must neither propagate nor skip the ledger write.
    await svc.record_completion(
        user_id="u1",
        session_id="s1",
        model_id="gpt-x",
        deployment=_deployment(),
        usage=_known_usage(),
    )
    summary = await svc.summarize("u1")
    assert summary.totalRequests == 1
