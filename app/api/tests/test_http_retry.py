"""Unit tests for the shared outbound transient-retry helper.

These exercise :func:`ai4ia_api.http_retry.request_with_retry` directly with a
fake single-attempt coroutine (no network, injected ``sleep``/``monotonic``), so
each retry rule is asserted in isolation: retry on 429/503 then succeed; never
retry other 4xx; never retry writes; honor Retry-After; enforce the
max-attempts cap; retry transient transport exceptions; and stop at the total
deadline.
"""
from __future__ import annotations

from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ai4ia_api.http_retry import RetryPolicy, parse_retry_after, request_with_retry


class _Sender:
    """A fake one-attempt sender that yields queued responses/exceptions in order."""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self) -> httpx.Response:
        self.calls += 1
        if not self._outcomes:
            raise AssertionError("sender called more times than expected")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, httpx.Response)
        return outcome


def _recording_sleep() -> tuple[list[float], object]:
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    return slept, _sleep


def _resp(status: int, *, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers, json={"status": status})


_FAST = RetryPolicy(max_attempts=3, backoff_base_seconds=0.0, backoff_max_seconds=0.0)


@pytest.mark.asyncio
async def test_retries_transient_status_then_succeeds():
    sender = _Sender([_resp(503), _resp(429), _resp(200)])
    slept, sleep = _recording_sleep()
    resp = await request_with_retry(sender, method="GET", policy=_FAST, sleep=sleep)
    assert resp.status_code == 200
    assert sender.calls == 3
    assert len(slept) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
async def test_does_not_retry_non_transient_4xx(status: int):
    sender = _Sender([_resp(status), _resp(200)])
    slept, sleep = _recording_sleep()
    resp = await request_with_retry(sender, method="GET", policy=_FAST, sleep=sleep)
    assert resp.status_code == status
    assert sender.calls == 1
    assert slept == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_does_not_retry_write_methods(method: str):
    sender = _Sender([_resp(503), _resp(200)])
    slept, sleep = _recording_sleep()
    resp = await request_with_retry(sender, method=method, policy=_FAST, sleep=sleep)
    assert resp.status_code == 503  # the single attempt's result, unretried
    assert sender.calls == 1
    assert slept == []


@pytest.mark.asyncio
async def test_honors_retry_after_seconds():
    sender = _Sender([_resp(503, headers={"Retry-After": "2"}), _resp(200)])
    slept, sleep = _recording_sleep()
    resp = await request_with_retry(sender, method="GET", policy=_FAST, sleep=sleep)
    assert resp.status_code == 200
    assert slept == [2.0]


@pytest.mark.asyncio
async def test_retry_after_is_capped():
    policy = RetryPolicy(max_attempts=2, retry_after_cap_seconds=10.0)
    sender = _Sender([_resp(503, headers={"Retry-After": "999"}), _resp(200)])
    slept, sleep = _recording_sleep()
    resp = await request_with_retry(sender, method="GET", policy=policy, sleep=sleep)
    assert resp.status_code == 200
    assert slept == [10.0]


@pytest.mark.asyncio
async def test_honors_retry_after_http_date():
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=3))
    sender = _Sender([_resp(503, headers={"Retry-After": when}), _resp(200)])
    slept, sleep = _recording_sleep()
    resp = await request_with_retry(sender, method="GET", policy=_FAST, sleep=sleep)
    assert resp.status_code == 200
    assert len(slept) == 1
    assert 0.0 <= slept[0] <= 5.0


@pytest.mark.asyncio
async def test_max_attempts_cap_enforced():
    sender = _Sender([_resp(503), _resp(503), _resp(503)])
    slept, sleep = _recording_sleep()
    resp = await request_with_retry(sender, method="GET", policy=_FAST, sleep=sleep)
    assert resp.status_code == 503
    assert sender.calls == 3  # exactly max_attempts, no more
    assert len(slept) == 2


@pytest.mark.asyncio
async def test_single_attempt_policy_runs_once():
    sender = _Sender([_resp(503), _resp(200)])
    slept, sleep = _recording_sleep()
    policy = RetryPolicy(max_attempts=1)
    resp = await request_with_retry(sender, method="GET", policy=policy, sleep=sleep)
    assert resp.status_code == 503
    assert sender.calls == 1
    assert slept == []


@pytest.mark.asyncio
async def test_retries_transient_exception_then_succeeds():
    sender = _Sender(
        [httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), _resp(200)]
    )
    slept, sleep = _recording_sleep()
    resp = await request_with_retry(sender, method="GET", policy=_FAST, sleep=sleep)
    assert resp.status_code == 200
    assert sender.calls == 3
    assert len(slept) == 2


@pytest.mark.asyncio
async def test_exhausted_transient_exception_propagates():
    sender = _Sender([httpx.ConnectTimeout("x"), httpx.ConnectTimeout("x"),
                      httpx.ConnectTimeout("x")])
    slept, sleep = _recording_sleep()
    with pytest.raises(httpx.ConnectTimeout):
        await request_with_retry(sender, method="GET", policy=_FAST, sleep=sleep)
    assert sender.calls == 3


@pytest.mark.asyncio
async def test_non_transient_exception_not_retried():
    sender = _Sender([httpx.InvalidURL("bad"), _resp(200)])
    slept, sleep = _recording_sleep()
    with pytest.raises(httpx.InvalidURL):
        await request_with_retry(sender, method="GET", policy=_FAST, sleep=sleep)
    assert sender.calls == 1


@pytest.mark.asyncio
async def test_total_deadline_stops_retry_early():
    # A monotonic clock that jumps past the deadline on the first check, so the
    # helper must give up rather than sleep + retry even though attempts remain.
    ticks = iter([0.0, 100.0, 100.0, 100.0])

    def _clock() -> float:
        try:
            return next(ticks)
        except StopIteration:
            return 100.0

    sender = _Sender([_resp(503, headers={"Retry-After": "0"}), _resp(200)])
    slept, sleep = _recording_sleep()
    policy = RetryPolicy(max_attempts=3, deadline_seconds=20.0)
    resp = await request_with_retry(
        sender, method="GET", policy=policy, sleep=sleep, monotonic=_clock
    )
    assert resp.status_code == 503
    assert sender.calls == 1
    assert slept == []


# --------------------------------------------------------------------------- #
# parse_retry_after: public so ai4ia_api.websearch.client can reuse the exact
# same Retry-After parsing for the Web IQ SDK's BrowseResponse.retryAfter field
# (same delta-seconds / HTTP-date shape, but not an httpx.Response header).
# --------------------------------------------------------------------------- #
def test_parse_retry_after_parses_delta_seconds():
    assert parse_retry_after("5") == 5.0


def test_parse_retry_after_parses_http_date():
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=3))
    seconds = parse_retry_after(when)
    assert seconds is not None
    assert 0.0 <= seconds <= 5.0


def test_parse_retry_after_rejects_empty_or_blank():
    assert parse_retry_after("") is None
    assert parse_retry_after("   ") is None


def test_parse_retry_after_rejects_unparseable_value():
    assert parse_retry_after("not-a-number-or-date") is None


def test_parse_retry_after_is_reused_by_websearch_client():
    # Guards against silently re-privatizing this helper: the websearch client
    # wrapper imports this exact function to parse an in-progress on-demand
    # crawl's retryAfter value, not just this module's own Retry-After header.
    from ai4ia_api.websearch import client as websearch_client

    assert websearch_client.parse_retry_after is parse_retry_after
