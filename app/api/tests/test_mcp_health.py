"""Tests for the pure MCP health/quarantine state machine + observability hooks
(Phase 12B Increment D).

These cover ``mcp_health`` (a dependency-free state machine over the durable
:class:`UserMcpServer` record) and ``mcp_observability`` (structured, redacted
log events). Both are framework-agnostic — no ToolExecutor, no I/O — so the tests
are plain and fast.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ai4ia_api.agents import mcp_health as health
from ai4ia_api.agents import mcp_observability as obs
from ai4ia_api.agents.mcp_health import (
    QUARANTINE_BASE_SECONDS,
    QUARANTINE_MAX_SECONDS,
    QUARANTINE_THRESHOLD,
    McpHealthStatus,
)
from ai4ia_api.agents.mcp_servers import UserMcpServer

_T0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _server(**over) -> UserMcpServer:
    body = dict(
        id="weather",
        userId="u1",
        name="weather",
        displayName="Weather",
        endpoint="https://mcp.example.com/rpc",
        host="mcp.example.com",
    )
    body.update(over)
    return UserMcpServer(**body)


# --- record_success / record_failure -----------------------------------------


def test_record_success_on_healthy_server_reports_no_change():
    server = _server()
    changed = health.record_success(server, now=_T0)
    # Healthy hot path: lastHealthCheck refreshed in memory but no forced write.
    assert changed is False
    assert server.lastHealthCheck == _T0
    assert server.consecutiveFailures == 0


def test_record_failure_increments_and_summarizes():
    server = _server()
    changed = health.record_failure(server, "connection refused", now=_T0)
    assert changed is True
    assert server.consecutiveFailures == 1
    assert server.lastHealthCheck == _T0
    assert "connection refused" in (server.lastHealthError or "")
    # One failure is below the threshold -> not yet quarantined.
    assert server.quarantinedUntil is None
    assert health.is_quarantined(server, now=_T0) is False


def test_quarantines_at_threshold():
    server = _server()
    for _ in range(QUARANTINE_THRESHOLD):
        health.record_failure(server, "boom", now=_T0)
    assert server.consecutiveFailures == QUARANTINE_THRESHOLD
    assert server.quarantinedUntil is not None
    assert health.is_quarantined(server, now=_T0) is True
    # First quarantine uses exactly the base window.
    assert server.quarantinedUntil == _T0 + timedelta(seconds=QUARANTINE_BASE_SECONDS)


def test_quarantine_window_backoff_is_exponential_and_capped():
    # At threshold: base. One past: 2x base. Capped at the max.
    assert health.quarantine_window(QUARANTINE_THRESHOLD) == timedelta(
        seconds=QUARANTINE_BASE_SECONDS
    )
    assert health.quarantine_window(QUARANTINE_THRESHOLD + 1) == timedelta(
        seconds=QUARANTINE_BASE_SECONDS * 2
    )
    assert health.quarantine_window(QUARANTINE_THRESHOLD + 50) == timedelta(
        seconds=QUARANTINE_MAX_SECONDS
    )


def test_record_success_clears_quarantine_and_reports_change():
    server = _server()
    for _ in range(QUARANTINE_THRESHOLD):
        health.record_failure(server, "boom", now=_T0)
    assert health.is_quarantined(server, now=_T0) is True

    changed = health.record_success(server, now=_T0 + timedelta(seconds=10))
    assert changed is True
    assert server.consecutiveFailures == 0
    assert server.quarantinedUntil is None
    assert server.lastHealthError is None
    assert health.is_quarantined(server, now=_T0 + timedelta(seconds=10)) is False


def test_auto_recovery_after_window_elapses():
    server = _server()
    for _ in range(QUARANTINE_THRESHOLD):
        health.record_failure(server, "boom", now=_T0)
    later = _T0 + timedelta(seconds=QUARANTINE_BASE_SECONDS + 1)
    # The quarantine window has elapsed -> server is reachable again without any
    # explicit reset (auto-recovery).
    assert health.is_quarantined(server, now=later) is False
    assert health.quarantine_remaining(server, now=later) is None


def test_quarantine_remaining_and_reason():
    server = _server()
    for _ in range(QUARANTINE_THRESHOLD):
        health.record_failure(server, "connection refused", now=_T0)
    remaining = health.quarantine_remaining(server, now=_T0)
    assert remaining == timedelta(seconds=QUARANTINE_BASE_SECONDS)
    reason = health.quarantine_reason(server, now=_T0)
    assert reason is not None
    assert "quarantined" in reason
    assert str(QUARANTINE_THRESHOLD) in reason
    assert "connection refused" in reason


def test_quarantine_reason_none_when_not_quarantined():
    assert health.quarantine_reason(_server(), now=_T0) is None
    assert health.quarantine_remaining(_server(), now=_T0) is None


def test_clear_quarantine_resets_state():
    server = _server()
    for _ in range(QUARANTINE_THRESHOLD):
        health.record_failure(server, "boom", now=_T0)
    changed = health.clear_quarantine(server)
    assert changed is True
    assert server.consecutiveFailures == 0
    assert server.quarantinedUntil is None
    # Idempotent: a second clear reports no change.
    assert health.clear_quarantine(server) is False


# --- health_status ------------------------------------------------------------


def test_health_status_unknown_then_healthy():
    assert health.health_status(_server(), now=_T0) is McpHealthStatus.unknown
    connected = _server(lastConnectedAt=_T0)
    assert health.health_status(connected, now=_T0) is McpHealthStatus.healthy


def test_health_status_degraded_then_quarantined():
    server = _server()
    health.record_failure(server, "boom", now=_T0)
    assert health.health_status(server, now=_T0) is McpHealthStatus.degraded
    for _ in range(QUARANTINE_THRESHOLD - 1):
        health.record_failure(server, "boom", now=_T0)
    assert health.health_status(server, now=_T0) is McpHealthStatus.quarantined


# --- summarize_error redaction ------------------------------------------------


def test_summarize_error_redacts_secret_material():
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    summary = health.summarize_error(f"401 from server token={secret}")
    assert secret not in summary
    assert "***REDACTED***" in summary


def test_summarize_error_is_bounded():
    summary = health.summarize_error("x" * 5000)
    assert len(summary) <= health.MAX_HEALTH_ERROR_LEN


# --- observability ------------------------------------------------------------


def test_emit_redacts_secret_in_detail(caplog):
    secret = "supersecretvalue_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    with caplog.at_level(logging.INFO, logger="ai4ia_api.agents.mcp"):
        obs.emit(
            event=obs.EVENT_TOOL_CALL,
            server="weather",
            host="mcp.example.com",
            tool="get_forecast",
            outcome=obs.OUTCOME_ERROR,
            detail=f"auth failed token={secret}",
        )
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert secret not in joined
    assert "***REDACTED***" in joined
    assert "event=mcp.tool_call" in joined


def test_emit_never_raises_on_bad_input():
    # A non-serializable detail / odd types must not propagate out of emit.
    obs.emit(event=obs.EVENT_DISCOVER, server="weather", latency_ms=None, detail=None)


def test_emit_quarantine_and_skip(caplog):
    with caplog.at_level(logging.INFO, logger="ai4ia_api.agents.mcp"):
        obs.emit_quarantine(
            server="weather",
            host="mcp.example.com",
            consecutive_failures=3,
            reason="repeated connection failures",
        )
        obs.emit_skip(server="weather", host="mcp.example.com", reason="quarantined ~5m")
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "event=mcp.quarantine" in messages
    assert "event=mcp.skip" in messages
