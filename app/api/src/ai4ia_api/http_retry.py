"""Conservative transient-retry for our bespoke outbound ``httpx`` clients.

This helper retries **only idempotent reads** (``GET``/``HEAD``/``OPTIONS``) on
**only transient failures** — connection/timeout errors and HTTP
``429``/``502``/``503``/``504`` — using capped exponential backoff with full
jitter and honoring an upstream ``Retry-After`` when present. Two hard caps keep
a dead dependency failing *fast* rather than hanging: a small ``max_attempts``
count and a bounded total ``deadline_seconds``.

It is deliberately a thin wrapper around a single-attempt coroutine, **not** an
``httpx`` transport, so it applies surgically at the exact call sites we know are
safe to repeat and leaves every other request path byte-for-byte unchanged.

Why not writes? Retrying a ``POST``/``PUT``/``PATCH``/``DELETE`` that may have
already succeeded server-side (only its response was lost) can duplicate the
effect — a second image/chat/embedding generation that double-bills, or a second
submitted job. So writes are run exactly once and their result/exception
propagate unchanged. Azure SDK call sites (cosmos/search/blob/keyvault) are not
wrapped at all: those SDKs ship their own retry policies and stacking another
layer is wrong.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

# Idempotent HTTP methods that are safe to repeat. Writes are intentionally
# excluded; see the module docstring.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Transient upstream statuses worth a retry: rate limiting (429) plus the
# gateway/load-balancer "try again" family (502/503/504). No other 4xx is ever
# retried — a 400/401/403/404/409 is a deterministic client/permission/shape
# error that a retry would only repeat.
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})

# httpx transport exceptions that signal a transient connection/timeout failure
# (as opposed to a protocol misuse or an SSRF/policy refusal) and so are safe to
# retry on an idempotent request.
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


@dataclass(frozen=True)
class RetryPolicy:
    """Caps + backoff shape for :func:`request_with_retry`.

    ``max_attempts`` counts *total* tries (so ``3`` means the first attempt plus
    up to two retries). ``deadline_seconds`` bounds the wall-clock spent across
    all attempts + backoff so a dead dependency fails fast. The backoff fields
    bound the jittered exponential delay between attempts; ``retry_after_cap_
    seconds`` caps how long an upstream ``Retry-After`` can make us wait.
    """

    max_attempts: int = 3
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    deadline_seconds: float = 20.0
    retry_after_cap_seconds: float = 10.0


def _backoff_delay(attempt: int, policy: RetryPolicy) -> float:
    """Full-jitter capped exponential backoff for a 1-based ``attempt`` index."""
    capped = min(
        policy.backoff_max_seconds,
        policy.backoff_base_seconds * (2 ** (attempt - 1)),
    )
    if capped <= 0:
        return 0.0
    return random.uniform(0.0, capped)


def _parse_retry_after(value: str) -> float | None:
    """Parse a ``Retry-After`` value (delta-seconds or an HTTP-date) to seconds."""
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _retry_after_delay(response: httpx.Response, policy: RetryPolicy) -> float | None:
    """The capped ``Retry-After`` delay for ``response``, or ``None`` if absent."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    seconds = _parse_retry_after(raw)
    if seconds is None:
        return None
    return min(seconds, policy.retry_after_cap_seconds)


async def request_with_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    method: str,
    policy: RetryPolicy,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> httpx.Response:
    """Run ``send`` with conservative transient retries for an idempotent request.

    ``send`` must perform exactly one HTTP attempt and return its
    :class:`httpx.Response`; it is re-invoked on each retry, so it should build a
    fresh request every call (relevant only for replayable, idempotent reads).

    Retries happen only when ``method`` is idempotent and the failure is
    transient — a :data:`RETRYABLE_EXCEPTIONS` transport error or a
    :data:`RETRYABLE_STATUS` response. Backoff honors ``Retry-After`` when the
    server sends one, else uses capped exponential backoff with full jitter, and
    never exceeds the policy's total ``deadline_seconds``. For non-idempotent
    methods (or a single-attempt policy) ``send`` runs exactly once and its
    result/exception propagate unchanged.
    """
    if method.upper() not in IDEMPOTENT_METHODS or policy.max_attempts <= 1:
        return await send()

    start = monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            response = await send()
        except RETRYABLE_EXCEPTIONS:
            if attempt >= policy.max_attempts:
                raise
            delay = _backoff_delay(attempt, policy)
            if not _within_deadline(start, delay, policy, monotonic):
                raise
            await sleep(delay)
            continue

        if response.status_code in RETRYABLE_STATUS and attempt < policy.max_attempts:
            delay = _retry_after_delay(response, policy)
            if delay is None:
                delay = _backoff_delay(attempt, policy)
            if _within_deadline(start, delay, policy, monotonic):
                await sleep(delay)
                continue
        return response


def _within_deadline(
    start: float,
    delay: float,
    policy: RetryPolicy,
    monotonic: Callable[[], float],
) -> bool:
    """True if sleeping ``delay`` then retrying stays inside the total deadline."""
    return (monotonic() - start) + delay <= policy.deadline_seconds
