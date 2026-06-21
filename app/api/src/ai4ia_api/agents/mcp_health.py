"""MCP server health + quarantine state machine.

A user-registered MCP server can become unreachable (the host goes down, a
credential is revoked, DNS rebinds to an internal address). Without a circuit
breaker every chat turn that attaches one of its tools would re-attempt the dead
server, paying the full connect timeout each time. This module is the small,
framework-agnostic state machine that tracks per-server health on the durable
:class:`~ai4ia_api.agents.mcp_servers.UserMcpServer` record and decides when to
*quarantine* a server (skip it with a clear reason) and when to let it back in.

Deliberately dependency-free: no ToolExecutor, no agent framework, no I/O. It
only reads/mutates the health fields on the record and exposes pure predicates.
The execution path (``mcp_execution``) consults :func:`is_quarantined` to gate,
and the service (``mcp_service``) calls :func:`record_success` /
:func:`record_failure` and persists the record when they report a change — so a
healthy server's hot path performs **zero** extra writes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

from .mcp_servers import UserMcpServer
from .tools import redact

# Consecutive transport failures tolerated before a server is quarantined.
QUARANTINE_THRESHOLD = 3
# Quarantine window grows with each failure past the threshold (exponential
# backoff) so a flapping server is retried sparingly, capped so it can
# auto-recover after the window elapses.
QUARANTINE_BASE_SECONDS = 300  # 5 minutes
QUARANTINE_MAX_SECONDS = 3600  # 1 hour
# Bound on the stored, redacted error summary.
MAX_HEALTH_ERROR_LEN = 280


class McpHealthStatus(str, Enum):
    """A server's coarse health, derived from its tracked failure state."""

    healthy = "healthy"  # last observation succeeded (or never failed)
    degraded = "degraded"  # some recent failures, not yet quarantined
    quarantined = "quarantined"  # skipped until the window elapses
    unknown = "unknown"  # never connected / no observation yet


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def quarantine_window(consecutive_failures: int) -> timedelta:
    """Backoff window for a server with ``consecutive_failures`` failures.

    The first quarantine (at the threshold) uses the base window; each additional
    failure doubles it, capped at :data:`QUARANTINE_MAX_SECONDS`.
    """
    over = max(0, consecutive_failures - QUARANTINE_THRESHOLD)
    seconds = QUARANTINE_BASE_SECONDS * (2**over)
    return timedelta(seconds=min(seconds, QUARANTINE_MAX_SECONDS))


def is_quarantined(server: UserMcpServer, *, now: datetime | None = None) -> bool:
    """True if the server is currently quarantined (window not yet elapsed)."""
    until = server.quarantinedUntil
    if until is None:
        return False
    return _now(now) < until


def quarantine_remaining(
    server: UserMcpServer, *, now: datetime | None = None
) -> timedelta | None:
    """Time left on the quarantine, or ``None`` if not quarantined."""
    until = server.quarantinedUntil
    if until is None:
        return None
    remaining = until - _now(now)
    return remaining if remaining.total_seconds() > 0 else None


def quarantine_reason(
    server: UserMcpServer, *, now: datetime | None = None
) -> str | None:
    """A human-readable reason a server is being skipped, or ``None``.

    Used both for the structured skip log and (mirrored) for the UI badge.
    """
    remaining = quarantine_remaining(server, now=now)
    if remaining is None:
        return None
    secs = int(remaining.total_seconds())
    when = f"{secs // 60}m" if secs >= 60 else f"{secs}s"
    why = server.lastHealthError or "repeated connection failures"
    return f"quarantined for ~{when} after {server.consecutiveFailures} failures: {why}"


def health_status(
    server: UserMcpServer, *, now: datetime | None = None
) -> McpHealthStatus:
    """Derive the coarse :class:`McpHealthStatus` from the tracked state."""
    if is_quarantined(server, now=now):
        return McpHealthStatus.quarantined
    if server.consecutiveFailures > 0:
        return McpHealthStatus.degraded
    if server.lastHealthCheck is None and server.lastConnectedAt is None:
        return McpHealthStatus.unknown
    return McpHealthStatus.healthy


def summarize_error(error: object) -> str:
    """A bounded, secret-redacted one-line summary of a failure."""
    text = " ".join(str(error).split())
    text = redact(text)
    if len(text) > MAX_HEALTH_ERROR_LEN:
        text = text[: MAX_HEALTH_ERROR_LEN - 1].rstrip() + "…"
    return text


def record_success(server: UserMcpServer, *, now: datetime | None = None) -> bool:
    """Note a successful connect/execute, clearing any failure/quarantine state.

    Returns ``True`` when the record materially changed (so the caller should
    persist it). A server that was already healthy returns ``False`` — its
    ``lastHealthCheck`` is refreshed in memory but no write is forced, keeping the
    healthy hot path write-free.
    """
    changed = bool(
        server.consecutiveFailures
        or server.quarantinedUntil is not None
        or server.lastHealthError is not None
    )
    server.consecutiveFailures = 0
    server.quarantinedUntil = None
    server.lastHealthError = None
    server.lastHealthCheck = _now(now)
    return changed


def record_failure(
    server: UserMcpServer, error: object, *, now: datetime | None = None
) -> bool:
    """Note a connect/execute failure; quarantine once the threshold is reached.

    Always reports a change (the failure count/summary advanced), so the caller
    persists the record. When ``consecutiveFailures`` reaches
    :data:`QUARANTINE_THRESHOLD` the server is quarantined for an escalating
    :func:`quarantine_window`.
    """
    moment = _now(now)
    server.consecutiveFailures += 1
    server.lastHealthCheck = moment
    server.lastHealthError = summarize_error(error)
    if server.consecutiveFailures >= QUARANTINE_THRESHOLD:
        server.quarantinedUntil = moment + quarantine_window(server.consecutiveFailures)
    return True


def clear_quarantine(server: UserMcpServer) -> bool:
    """Force a server out of quarantine and reset its failure count.

    Returns ``True`` if anything changed. Used by the explicit user-initiated
    reconnect path so a manual "Test" is never blocked by a stale quarantine.
    """
    changed = bool(server.consecutiveFailures or server.quarantinedUntil is not None)
    server.consecutiveFailures = 0
    server.quarantinedUntil = None
    return changed
