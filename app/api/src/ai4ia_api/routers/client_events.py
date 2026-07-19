"""Browser-side telemetry beacon.

The web app has no first-party client exception/event telemetry today: chat
render, microphone-capture, and TTS-playback failures are only visible to us
via Application Insights on the *backend* (:mod:`ai4ia_api.logging_setup`),
so a browser-only failure with a healthy upstream is invisible until a user
reports it. This router gives the browser a narrow, best-effort way to land
a small, bounded set of client-observed failures in that SAME customEvents
pipeline, instead of adding a new telemetry stack.

Content-free by construction, not by sanitization. Earlier versions of this
endpoint accepted free-text ``message``/``route``/``component`` fields and
ran them through a regex redaction pass (percent-decoding, JSON-unescaping,
auth-scheme matching, etc.) before logging them. Across several review
rounds that sanitizer kept getting bypassed by a new encoding/nesting/quoting
shape (URL-encoded delimiters, doubly-encoded delimiters, JSON-nested
credentials, unterminated quotes, standalone scheme+credential pairs...) --
an unwinnable arms race, because the set of ways to obscure a substring from
a regex is unbounded. The fix is to never accept free text at all:

- ``event`` is a small fixed enum (:data:`ClientEventType`).
- ``code`` is re-validated against a small fixed allowlist
  (:data:`_KNOWN_CODES`) and coerced to ``"unknown"`` if it doesn't match --
  never trusted/logged as free text, even though it's a string on the wire.
- ``severity`` is a small fixed enum (``ClientEventSeverity``).
- ``hasDigest`` is a plain boolean.
- ``model_config = ConfigDict(extra="forbid")`` on :class:`ClientEventReport`
  makes any *other* field a hard 422 rejection, so a modified/compromised
  client cannot smuggle a ``message``/``route``/anything-else field past this
  model no matter how it encodes it -- there is no sanitizer to bypass
  because there is no free-text field left to sanitize.

This also structurally resolves an earlier bug where a raw client
``message`` was passed to the logger under the reserved ``"message"``
``extra`` key, which ``logging.Logger.makeRecord`` rejects with a
``KeyError`` that was previously swallowed, silently dropping every event
that carried one: there is no longer any free-text content passed to the
logger at all, so there's nothing left that could collide with a reserved
``LogRecord`` attribute.

Deliberately minimal:
- No new telemetry SDK/dependency — reuses ``emit_custom_event`` exactly like
  chat completions, MCP tool calls, and document ingest already do.
- No Cosmos write: this is ephemeral operational telemetry, not canonical
  domain data (see AGENTS.md "Cosmos is canonical" — this deliberately isn't
  that).
- Auth required, like every other non-health route, plus a tiny in-memory
  per-user rate limit so a runaway retry loop in one tab can't flood App
  Insights.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..logging_setup import emit_custom_event

router = APIRouter(prefix="/api/client-events", tags=["client-events"])

# Small, stable taxonomy so App Insights customEvents stay queryable — extend
# here rather than accepting free-form event names from the browser.
ClientEventType = Literal[
    "render_error",
    "window_error",
    "unhandled_rejection",
    "media_playback_error",
    "microphone_error",
]

# Mirrors the browser's ClientEventSeverity (clientTelemetry.ts — keep in
# sync). Every current caller reports "error"; the other values exist so a
# future non-fatal event has somewhere to report without inventing a new
# event kind.
ClientEventSeverity = Literal["error", "warning", "info"]

# Stable, non-sensitive error classification. Mirrors the browser's own
# allowlist (clientTelemetry.ts's KNOWN_CODES — keep in sync). Re-validated
# here rather than trusted from the client: this field is a string on the
# wire, but it is never treated as free text -- anything outside this set is
# normalized to "unknown" below.
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

_MAX_CODE_LEN = 40


# Fixed-window per-user cap. In-memory/per-process is fine here — this is
# best-effort telemetry, not billing — generous enough for a genuinely broken
# session, tight enough that a runaway retry loop can't flood App Insights.
_RATE_LIMIT_PER_MINUTE = 20
_RATE_WINDOW_SECONDS = 60.0
_hits: dict[str, deque[float]] = {}
_hits_lock = threading.RLock()

# The per-call prune above only removes *timestamps inside* the current user's
# deque -- it never removes the dict *entry* itself, so a long-running process
# accumulates one entry per distinct user_id ever seen, forever, even once every
# one of them has been inactive for hours (unbounded memory growth). Sweep
# periodically (not on every call, to keep the common hot path cheap) to drop
# entries for users inactive longer than the rate window. The hard cap on
# total tracked users is enforced on every insertion in `_rate_limited` itself
# (see below) rather than only here, so a burst of distinct new users between
# sweeps can never transiently exceed `_MAX_TRACKED_USERS`.
_SWEEP_INTERVAL_SECONDS = 300.0
_MAX_TRACKED_USERS = 10_000
_last_sweep = 0.0


def _sweep_stale_users(now: float) -> None:
    """Drops ``_hits`` entries for users inactive longer than the rate
    window. Locks independently (reentrant, so it's also safe to call from
    inside ``_rate_limited`` while that function already holds the lock) so
    it can run correctly whether triggered periodically from a live request
    or driven directly -- e.g. by a separate scheduler/thread, or a test
    simulating one -- without requiring the caller to already hold the lock."""
    global _last_sweep
    with _hits_lock:
        stale = [
            user_id
            for user_id, window in _hits.items()
            if not window or now - window[-1] > _RATE_WINDOW_SECONDS
        ]
        for user_id in stale:
            del _hits[user_id]
        _last_sweep = now


def _rate_limited(user_id: str) -> bool:
    """Thread-safe fixed-window rate check with an LRU-bounded ``_hits``.

    All read-modify-write access to the shared ``_hits``/``_last_sweep``
    state is under one lock: `dict.items()` iteration in `_sweep_stale_users`
    would otherwise race with concurrent mutation from another thread (this
    endpoint is `async def`, but the underlying dict is also exercised
    directly, concurrently, by tests, and could in principle be driven from a
    worker thread), and the previous unlocked design only enforced
    `_MAX_TRACKED_USERS` inside the periodic sweep, so a burst of concurrent
    distinct users between sweeps could transiently exceed the cap.
    """
    now = time.monotonic()
    with _hits_lock:
        window = _hits.pop(user_id, None)
        if window is None:
            # A brand-new user_id: enforce the hard cap on *every* insertion
            # (not just periodically in _sweep_stale_users), evicting the
            # least-recently-touched entry first if already at capacity.
            # Every existing/touched user_id is re-inserted at the end of
            # `_hits` below, so plain dict insertion order already doubles as
            # LRU order here -- the front of the dict is always the entry
            # least recently touched.
            if len(_hits) >= _MAX_TRACKED_USERS:
                oldest = next(iter(_hits), None)
                if oldest is not None:
                    del _hits[oldest]
            window = deque()
        while window and now - window[0] > _RATE_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= _RATE_LIMIT_PER_MINUTE:
            limited = True
        else:
            window.append(now)
            limited = False
        # Re-insert at the end (the most-recently-touched position) whether
        # or not this call appended a timestamp, so LRU order reflects
        # activity, not just successful (non-rate-limited) calls.
        _hits[user_id] = window
        if now - _last_sweep > _SWEEP_INTERVAL_SECONDS:
            _sweep_stale_users(now)
    return limited


class ClientEventReport(BaseModel):
    """Content-free, allowlisted telemetry payload -- see module docstring.

    ``extra="forbid"`` is what actually keeps arbitrary/free-form content out
    of this endpoint (not per-field validation alone): a modified/compromised
    client cannot smuggle a ``message``, ``route``, or any other key past
    this model no matter how it's encoded or nested -- Pydantic rejects the
    whole request (422) before any handler code, including
    ``emit_custom_event``, ever sees it.
    """

    model_config = ConfigDict(extra="forbid")

    event: ClientEventType
    # Stable classification (e.g. "TypeError"); defaults/normalizes to
    # "unknown" rather than 422ing -- this is best-effort metadata, not a
    # security control, so an unrecognized value is coerced, not rejected.
    code: str = Field("unknown", max_length=_MAX_CODE_LEN)
    severity: ClientEventSeverity = "error"
    # Whether Next.js attached a correlatable digest to a render error --
    # NOT the digest value itself (a string), just its presence. There is no
    # field anywhere in this model wide enough to carry the digest, a
    # message, a URL, or any other free text.
    hasDigest: bool = False

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
                "severity": body.severity,
                "hasDigest": body.hasDigest,
                "userId": user.internal_user_id,
            },
        )
    return Response(status_code=status.HTTP_202_ACCEPTED)
