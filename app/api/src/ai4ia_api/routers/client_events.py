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
import threading
import time
from collections import deque
from typing import Literal
from urllib.parse import unquote

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
        #
        # Two more shapes handled here (regression coverage for a follow-up
        # review round): a leading/trailing `"?` around the label so a
        # JSON-serialized key like `{"Authorization":"Basic <cred>"}` -- where
        # a closing key-quote sits between the label and the `:` -- still
        # matches and both quotes are consumed (not left dangling in the
        # output); and a `"[^"]*"` alternative tried before the bare-token one
        # so a *quoted* credential is matched as one atomic unit even when the
        # scheme word right before it is unquoted, e.g.
        # `Authorization: Basic "<cred>"` (previously the bare-token
        # alternative excludes `"`, so backtracking gave up on the optional
        # scheme-word match and matched only "Basic", leaving the quoted
        # credential completely exposed).
        #
        # Three more shapes added in a later round: the quoted-value
        # alternative now has an OPTIONAL closing quote, so a value whose
        # closing quote is missing entirely is still consumed to the end of
        # the string rather than falling through to the bare-token
        # alternative (which excludes quote characters from its class and so
        # cannot even start matching at an opening quote, previously leaving
        # the whole thing unredacted); a bounded, single-level `{...}`
        # alternative so a value that is itself a small JSON object is
        # consumed as one atomic unit instead of the bare-token fallback
        # matching only its opening brace and leaving whatever is nested
        # inside fully exposed (deliberately one level deep, not truly
        # recursive -- `re` can't balance nested braces, and the field
        # length caps below bound how much nesting is realistic to worry
        # about further); and `credential` is added to the label list so a
        # bare, non-nested key of that name is caught on its own.
        #
        # `(?!\[redacted(?:-[a-z]+)?\])` immediately before the value
        # guards against re-matching this pattern's OWN prior output on a
        # later `_sanitize` pass (see MAX_SANITIZE_PASSES below): without
        # it, a value of literal `[redacted]` -- exactly what got written
        # here on a previous pass -- satisfies the bare-token alternative
        # just like a real credential would, and if that previous pass's
        # match happened to leave a legitimate leading quote in place (the
        # standalone pattern below does this deliberately), THIS pattern's
        # own optional leading `"?` can then wrongly reinterpret that quote
        # as a JSON-key-closing quote and consume-and-drop it, corrupting
        # already-correct output on the next pass.
        re.compile(
            r"\"?\b(authorization|bearer|token|api[_-]?key|secret|password|access[_-]?key|sas|credential)"
            r"\b\"?\s*[:=]\s*(?:(?:basic|bearer|digest|negotiate|ntlm|oauth)\s+)?"
            r"(?!\[redacted(?:-[a-z]+)?\])(?:\"[^\"]*\"?|\{[^{}]*\}|[^\s\"&,]+)",
            re.IGNORECASE,
        ),
        r"\1=[redacted]",
    ),
    (
        # Standalone scheme+credential with no "Authorization"/"token"-style
        # label at all (e.g. bare `Bearer <cred>`, `Basic: <cred>`, a
        # punctuation-adjacent `(Bearer "<cred>")`, or a JSON-nested
        # `["Bearer <cred>"]`) -- distinct from the pattern above, which
        # requires a label word before the scheme. `Basic`/`Digest`/
        # `Negotiate`/`NTLM`/`OAuth` are never label words above, so without
        # this they pass through untouched whenever they're not preceded by
        # "Authorization:" et al. Gated on a `:`/`=`/quote signal (or,
        # failing that, at least a 2+ char unquoted token after mandatory
        # whitespace) so an incidental trailing punctuation mark isn't
        # mistaken for a credential; this can still over-redact rare prose
        # like "Bearer bonds", an accepted tradeoff for a security sanitizer
        # where false negatives (a leaked credential) are far costlier than
        # false positives (a little lost diagnostic text). Runs after the
        # label-prefixed pattern above so the common "Authorization: Bearer
        # xyz" case is fully consumed there first, leaving nothing here.
        #
        # Same optional-closing-quote and bounded single-level `{...}`
        # alternatives as the label-prefixed pattern above, added in a later
        # round for the same reasons: an unterminated quoted value is still
        # consumed to the end of the string instead of leaking unmatched,
        # and a small nested JSON object following the scheme word is
        # consumed as one atomic unit instead of only its opening brace.
        # Same anti-re-match guard as the label-prefixed pattern above,
        # for the same reason (this pattern's own `word=[redacted]` output
        # would otherwise look like a fresh standalone credential on the
        # next pass).
        re.compile(
            r"\b(basic|bearer|digest|negotiate|ntlm|oauth)\b"
            r"(?:\s*[:=]\s*(?!\[redacted(?:-[a-z]+)?\])(?:\"[^\"]*\"?|\{[^{}]*\}|[^\s\"&,]+)"
            r"|\s+(?!\[redacted(?:-[a-z]+)?\])(?:\"[^\"]*\"?|\{[^{}]*\}|[^\s\"&,]{2,}))",
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


def _decode_percent_encoding(value: str) -> str:
    """Decodes %XX percent-encoding so a redaction pattern that expects a
    literal delimiter (":", "=", a space) isn't defeated by URL-encoding the
    delimiter away -- e.g. "token%3Dsecret" or "Basic%20secret" have no
    literal "="/space for the patterns above to match against. `unquote`
    never raises (invalid sequences pass through unchanged by default), so
    unlike the browser-side per-triple decoder this can safely operate on
    the whole string in one call."""
    return unquote(value)


# Bounds how many decode-then-redact rounds `_sanitize` runs (see below).
_MAX_SANITIZE_PASSES = 3


def _sanitize(text: str) -> str:
    """Strips control characters and redacts common secret/PII shapes
    (bearer/API tokens, URLs with query strings, emails, GUIDs, other long
    opaque tokens) from a free-text telemetry field. Best-effort, not a
    guarantee against every possible leak shape — combined with the field
    length caps and the browser-side sanitizer, this keeps the common,
    high-risk shapes (credentials, PII, session/request ids) out of
    Application Insights even if one layer is bypassed.

    Runs as a bounded decode-then-redact loop, not a single linear pass: a
    percent-encoded delimiter can itself be re-encoded a second time (e.g.
    "%2520" decodes to "%20", which itself still needs a further decode pass
    before it becomes a literal space), and the patterns above only
    recognize a *literal* delimiter, so one decode pass alone can leave a
    still-encoded credential unredacted. Each pass re-decodes, re-strips
    control characters revealed by decoding (e.g. a decoded "%0A" newline),
    and re-applies every pattern; the loop stops as soon as a pass produces
    no further change -- the common, non-adversarial case exits after one
    confirmatory pass. What is returned is always the result of that full
    pipeline: the decoded-but-not-yet-redacted intermediate is never what
    gets returned."""
    result = text
    for _ in range(_MAX_SANITIZE_PASSES):
        candidate = re.sub(r"[\r\n\t]+", " ", _decode_percent_encoding(result))
        for pattern, replacement in _REDACTIONS:
            candidate = pattern.sub(replacement, candidate)
        if candidate == result:
            result = candidate
            break
        result = candidate
    return result.strip()


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
