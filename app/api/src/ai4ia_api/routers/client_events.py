"""Browser-side telemetry beacon.

The web app has no first-party client exception/event telemetry today: chat
render, microphone-capture, and TTS-playback failures are only visible to us
via Application Insights on the *backend* (:mod:`ai4ia_api.logging_setup`),
so a browser-only failure with a healthy upstream is invisible until a user
reports it. This router gives the browser a narrow, best-effort way to land
a small, bounded set of client-observed failures in that SAME customEvents
pipeline, instead of adding a new telemetry stack.

Deliberately minimal, with two layers of content control:
- No new telemetry SDK/dependency — reuses ``emit_custom_event`` exactly like
  chat completions, MCP tool calls, and document ingest already do.
- No free-form payloads beyond a small event-type enum, a *stable, allowlisted*
  ``code`` (e.g. ``"TypeError"``/``"NotAllowedError"``; never free text — see
  ``_KNOWN_CODES``), and short, length-capped text fields. No stack traces, no
  request/response bodies. Free-text fields (``message``/``route``/
  ``component``) are independently redacted here (``_sanitize``) for common
  secret/PII shapes (tokens, URLs, emails, GUIDs) — the browser
  (``clientTelemetry.ts``) does the same, but a modified/compromised client
  could skip that, so this is defense-in-depth, not the only layer.
- No Cosmos write: this is ephemeral operational telemetry, not canonical
  domain data (see AGENTS.md "Cosmos is canonical" — this deliberately isn't
  that).
- Auth required, like every other non-health route, plus a tiny in-memory
  per-user rate limit so a runaway retry loop in one tab can't flood App
  Insights.
"""
from __future__ import annotations

import re
import time
from collections import deque
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field, field_validator

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..logging_setup import emit_custom_event

router = APIRouter(prefix="/api/client-events", tags=["client-events"])

# Small, stable taxonomy so App Insights customEvents stay queryable — extend
# here rather than accepting free-form event names from the browser.
ClientEventType = Literal[
    "render_error",
    "unhandled_error",
    "unhandled_rejection",
    "media_playback_error",
    "microphone_error",
]

# Stable, non-sensitive error classification. Mirrors the browser's own
# allowlist (clientTelemetry.ts's KNOWN_CODES — keep in sync); re-validated
# here rather than trusted from the client. Anything outside this set is
# normalized to "unknown" below, so this field can never carry free text.
_KNOWN_CODES = frozenset(
    {
        "Error",
        "TypeError",
        "RangeError",
        "ReferenceError",
        "SyntaxError",
        "URIError",
        "EvalError",
        "AbortError",
        "NetworkError",
        "TimeoutError",
        "QuotaExceededError",
        "NotAllowedError",
        "NotFoundError",
        "NotSupportedError",
        "SecurityError",
        "DOMException",
        "string_rejection",
        "non_error_rejection",
        "unknown",
    }
)

_MAX_MESSAGE_LEN = 300
_MAX_ROUTE_LEN = 200
_MAX_COMPONENT_LEN = 100
_MAX_CODE_LEN = 40

# Defense-in-depth redaction for the free-text fields, applied before they
# ever reach emit_custom_event/App Insights. Mirrors clientTelemetry.ts's
# REDACTIONS list — keep the two in sync. Order matters: broader patterns
# (JWTs, URLs) run before the generic long-opaque-token catch-all so a match
# isn't partially double-redacted.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "[redacted-token]",
    ),
    (
        # The optional (?:scheme\s+)? group consumes an HTTP auth scheme word
        # (e.g. "Authorization: Basic <credential>") together with the
        # credential that follows it, as ONE match. Without it, `[^\s"&,]+`
        # alone greedily stops at the first whitespace and matches just the
        # scheme word -- redacting "Basic"/"Bearer" while leaving the actual
        # credential completely untouched afterward.
        re.compile(
            r"\b(authorization|bearer|token|api[_-]?key|secret|password|access[_-]?key|sas)"
            r"\b\s*[:=]\s*\"?(?:(?:basic|bearer|digest|negotiate|ntlm|oauth)\s+)?[^\s\"&,]+",
            re.IGNORECASE,
        ),
        r"\1=[redacted]",
    ),
    (re.compile(r"https?://\S+", re.IGNORECASE), "[redacted-url]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[redacted-email]"),
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
        ),
        "[redacted-id]",
    ),
    (re.compile(r"\b[A-Za-z0-9+/_-]{24,}\b"), "[redacted-token]"),
]


def _sanitize(text: str) -> str:
    """Strips control characters and redacts common secret/PII shapes
    (bearer/API tokens, URLs with query strings, emails, GUIDs, other long
    opaque tokens) from a free-text telemetry field. Best-effort, not a
    guarantee against every possible leak shape — combined with the field
    length caps and the browser-side sanitizer, this keeps the common,
    high-risk shapes (credentials, PII, session/request ids) out of
    Application Insights even if one layer is bypassed."""
    result = re.sub(r"[\r\n\t]+", " ", text)
    for pattern, replacement in _REDACTIONS:
        result = pattern.sub(replacement, result)
    return result.strip()


# Fixed-window per-user cap. In-memory/per-process is fine here — this is
# best-effort telemetry, not billing — generous enough for a genuinely broken
# session, tight enough that a runaway retry loop can't flood App Insights.
_RATE_LIMIT_PER_MINUTE = 20
_RATE_WINDOW_SECONDS = 60.0
_hits: dict[str, deque[float]] = {}

# The per-call prune above only removes *timestamps inside* the current user's
# deque -- it never removes the dict *entry* itself, so a long-running process
# accumulates one entry per distinct user_id ever seen, forever, even once every
# one of them has been inactive for hours (unbounded memory growth). Sweep
# periodically (not on every call, to keep the common hot path O(1)) to drop
# entries for users inactive longer than the rate window, and hard-cap the dict
# as a backstop so a burst of distinct users between sweeps still can't grow
# this unbounded.
_SWEEP_INTERVAL_SECONDS = 300.0
_MAX_TRACKED_USERS = 10_000
_last_sweep = 0.0


def _sweep_stale_users(now: float) -> None:
    """Drop ``_hits`` entries for users inactive longer than the rate window,
    then enforce ``_MAX_TRACKED_USERS`` by evicting the least-recently-active
    entries first (oldest ``window[-1]`` = least recently active)."""
    global _last_sweep
    stale = [
        user_id
        for user_id, window in _hits.items()
        if not window or now - window[-1] > _RATE_WINDOW_SECONDS
    ]
    for user_id in stale:
        del _hits[user_id]
    overflow = len(_hits) - _MAX_TRACKED_USERS
    if overflow > 0:
        # Every surviving window is non-empty here (empties were removed
        # above), so window[-1] is always the user's most recent activity.
        by_recency = sorted(_hits.items(), key=lambda item: item[1][-1])
        for user_id, _window in by_recency[:overflow]:
            del _hits[user_id]
    _last_sweep = now


def _rate_limited(user_id: str) -> bool:
    now = time.monotonic()
    window = _hits.setdefault(user_id, deque())
    while window and now - window[0] > _RATE_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= _RATE_LIMIT_PER_MINUTE:
        limited = True
    else:
        window.append(now)
        limited = False
    # This function has no `await`, so it runs atomically w.r.t. other
    # coroutines on the event loop -- concurrent requests can't interleave the
    # sweep with another user's window mutation.
    if now - _last_sweep > _SWEEP_INTERVAL_SECONDS:
        _sweep_stale_users(now)
    return limited


class ClientEventReport(BaseModel):
    event: ClientEventType
    # Stable classification (e.g. "TypeError"); defaults/normalizes to
    # "unknown" rather than 422ing — this is best-effort metadata, not a
    # security control, so an unrecognized value is coerced, not rejected.
    code: str = Field("unknown", max_length=_MAX_CODE_LEN)
    message: str = Field("", max_length=_MAX_MESSAGE_LEN)
    route: str = Field("", max_length=_MAX_ROUTE_LEN)
    component: str = Field("", max_length=_MAX_COMPONENT_LEN)

    @field_validator("code")
    @classmethod
    def _normalize_code(cls, value: str) -> str:
        return value if value in _KNOWN_CODES else "unknown"


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def report_client_event(
    body: ClientEventReport,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    # A misbehaving tab shouldn't loop retrying a telemetry beacon, so an
    # over-cap report is dropped silently (202) rather than erroring.
    if not _rate_limited(user.internal_user_id):
        emit_custom_event(
            "client_event",
            {
                "source": "browser",
                "event": body.event,
                "code": body.code,
                # NOTE: the attribute key is deliberately "clientMessage", not
                # "message" — `logging.Logger.makeRecord` reserves "message"
                # (and "asctime") on LogRecord and raises KeyError for any
                # `extra` dict that reuses them. That raise was previously
                # swallowed by emit_custom_event's blanket except-pass, so
                # every report with a non-empty message silently vanished
                # before reaching Application Insights, even though this
                # endpoint still returned 202. See test_client_events_api.py's
                # real-logger-path test, which exercises this without mocking
                # emit_custom_event so a future regression can't hide the
                # same way.
                "clientMessage": _sanitize(body.message) or None,
                "route": _sanitize(body.route) or None,
                "component": _sanitize(body.component) or None,
                "userId": user.internal_user_id,
            },
        )
    return Response(status_code=status.HTTP_202_ACCEPTED)
