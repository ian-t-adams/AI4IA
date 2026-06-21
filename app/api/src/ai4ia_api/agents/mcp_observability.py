"""MCP observability hooks: structured, redacted events.

A tiny, framework-agnostic helper that emits one structured log line per MCP
boundary event (discovery, per-tool execution, quarantine transitions) carrying
metrics-ready fields — ``event``, ``server``, ``host``, ``tool``, ``outcome``,
``latency_ms`` — so a log-based metrics pipeline can aggregate latency and
success/failure rates without any new telemetry backend. The app has no metrics
sink wired today, so structured logs are the seam; this module is the single
place that formats them.

Two hard rules, enforced here so callers can't get them wrong:

* **Never log secrets.** Any free-text ``detail`` is run through the existing
  :func:`~ai4ia_api.agents.tools.redact` before it is attached, mirroring the
  redaction the tool registry already applies to tool I/O.
* **Never break a turn.** Emitting telemetry is best-effort; a logging failure is
  swallowed (the work the telemetry describes has already happened or failed on
  its own terms).

It instruments the ``mcp_client``/``mcp_service`` boundary and the ``mcp_execution``
seam only — it holds no ToolExecutor/agent-framework references, preserving the
separability invariant.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from .tools import redact

logger = logging.getLogger("ai4ia_api.agents.mcp")

# Stable event names (the ``event`` field) so log-based metrics can group on them.
EVENT_DISCOVER = "mcp.discover"
EVENT_TOOL_CALL = "mcp.tool_call"
EVENT_QUARANTINE = "mcp.quarantine"
EVENT_SKIP = "mcp.skip"

# Outcomes for the ``outcome`` field.
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_TOOL_ERROR = "tool_error"  # server reachable, tool returned isError


class Timer:
    """Monotonic millisecond timer for a single boundary call."""

    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.monotonic()

    @property
    def ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)


@contextmanager
def timed() -> Iterator[Timer]:
    """Context manager yielding a :class:`Timer` for ``latency_ms`` measurement."""
    yield Timer()


def emit(
    *,
    event: str,
    server: str,
    host: str | None = None,
    tool: str | None = None,
    outcome: str | None = None,
    latency_ms: int | None = None,
    detail: str | None = None,
    level: int | None = None,
) -> None:
    """Emit one structured MCP event. Secrets in ``detail`` are redacted.

    ``level`` defaults to WARNING for error outcomes and INFO otherwise. Failures
    to log are swallowed — telemetry must never break a turn.
    """
    try:
        fields: dict[str, object] = {"event": event, "server": server}
        if host:
            fields["host"] = host
        if tool is not None:
            fields["tool"] = tool
        if outcome is not None:
            fields["outcome"] = outcome
        if latency_ms is not None:
            fields["latency_ms"] = latency_ms
        if detail:
            fields["detail"] = redact(detail)
        if level is None:
            level = logging.WARNING if outcome == OUTCOME_ERROR else logging.INFO
        # ``key=value`` rendering keeps the line greppable and metrics-parseable.
        message = " ".join(f"{k}={v}" for k, v in fields.items())
        logger.log(level, message, extra={"mcp": fields})
    except Exception:  # noqa: BLE001 - observability must never raise
        pass


def emit_quarantine(
    *, server: str, host: str | None, consecutive_failures: int, reason: str | None
) -> None:
    """Emit the transition into quarantine (a notable, low-frequency event)."""
    emit(
        event=EVENT_QUARANTINE,
        server=server,
        host=host,
        outcome=OUTCOME_ERROR,
        detail=reason or f"{consecutive_failures} consecutive failures",
        level=logging.WARNING,
    )


def emit_skip(*, server: str, host: str | None, reason: str) -> None:
    """Emit a per-turn skip of a quarantined server (gate fired)."""
    emit(
        event=EVENT_SKIP,
        server=server,
        host=host,
        outcome=OUTCOME_ERROR,
        detail=reason,
        level=logging.INFO,
    )
