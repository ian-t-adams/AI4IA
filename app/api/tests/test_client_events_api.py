"""Client-events beacon: auth gating, the content-free allowlisted schema,
rate limiting, and the ``emit_custom_event`` bridge (mirrors
``test_telemetry.py``'s style for asserting the customEvents contract).

Several review rounds found that a free-text telemetry field can always be
adversarially encoded/nested/quoted to bypass regex-based redaction, so the
schema itself no longer has a free-text field: ``event``/``code``/
``severity`` are small fixed enums (``code`` normalizes anything unmatched to
"unknown" instead of being trusted verbatim) and ``hasDigest`` is a plain
boolean. ``ClientEventReport`` also sets ``extra="forbid"``, so any field
outside that set -- named anything, containing anything, however deeply
nested or encoded -- is rejected (422) before request handling even begins.
The hostile-input tests below reuse the exact adversarial corpus that used to
defeat the regex sanitizer, now aimed at the two remaining surfaces (an
illegal extra field, and the one legal string field, ``code``), proving
neither can carry that content into ``emit_custom_event``/the logger.
"""
from __future__ import annotations

import logging
import threading
from collections import deque

import pytest

from ai4ia_api import logging_setup
from ai4ia_api.auth.base import AuthError

# Adversarial corpus reused across the hostile-input tests below. Mirrors
# clientTelemetry.test.ts's hostile `code` fixtures. Intentionally
# low-entropy, synthetic placeholders (never realistic-looking secrets) so
# they read clearly as test data, not real credentials.
_HOSTILE_STRINGS = [
    "Authorization: Basic YWxpY2U6cGFzc3dvcmQ=",
    "Authorization%3A%20Basic%20YWxpY2U6cGFzc3dvcmQ%3D",
    "Basic%2520YWxpY2U6cGFzc3dvcmQ%253D",  # double percent-encoded
    '{"Authorization":"Basic YWxpY2U6cGFzc3dvcmQ="}',
    'Authorization: Basic "YWxpY2U6cGFzc3dvcmQ=',  # unterminated quote
    "TypeError\x00\x01 with control chars",
    "TypeError'; DROP TABLE users;--",
    "a" * 5000,
    "https://example.test/reset?token=abc123",
    "user@example.test leaked here",
    "Bearer " + "z" * 40,
]

# Subset of the corpus above short enough to pass the `code` field's own
# max_length -- used for the "normalizes to unknown" test, since a value that
# exceeds max_length is rejected outright before normalization ever runs (see
# test_overlong_code_value_is_rejected_outright, which covers that case
# separately).
_HOSTILE_STRINGS_WITHIN_CODE_LENGTH = [s for s in _HOSTILE_STRINGS if len(s) <= 40]


class _RaisingAuth:
    """Auth provider that always rejects — simulates an anonymous request."""

    async def authenticate(self, credentials):
        raise AuthError("missing credentials")


def test_rejects_anonymous_request(client):
    client.app.state.auth_provider = _RaisingAuth()
    resp = client.post("/api/client-events", json={"event": "render_error"})
    assert resp.status_code == 401


def test_accepts_minimal_valid_report(client, monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post(
        "/api/client-events",
        json={"event": "render_error", "code": "TypeError", "severity": "warning", "hasDigest": True},
        headers={"X-Dev-User": "alice"},
    )
    assert resp.status_code == 202
    assert len(captured) == 1
    name, attrs = captured[0]
    assert name == "client_event"
    assert attrs == {
        "source": "browser",
        "event": "render_error",
        "code": "TypeError",
        "severity": "warning",
        "hasDigest": True,
        "userId": attrs["userId"],
    }
    assert attrs["userId"]


def test_defaults_apply_when_optional_fields_omitted(client, monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post("/api/client-events", json={"event": "microphone_error"})
    assert resp.status_code == 202
    _, attrs = captured[0]
    assert attrs["code"] == "unknown"
    assert attrs["severity"] == "error"
    assert attrs["hasDigest"] is False


def test_rejects_unknown_event_type(client, monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post("/api/client-events", json={"event": "made_up_event"})
    assert resp.status_code == 422
    assert captured == []


@pytest.mark.parametrize("hostile", _HOSTILE_STRINGS)
def test_hostile_event_value_is_rejected(client, monkeypatch, hostile):
    """`event` is a closed Literal -- any value outside the fixed enum is a
    422, regardless of length, encoding, or nesting (Literal validation is a
    plain equality check, not a length- or pattern-bounded one)."""
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post("/api/client-events", json={"event": hostile})
    assert resp.status_code == 422
    assert captured == []


def test_rejects_invalid_severity_value(client, monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post(
        "/api/client-events", json={"event": "render_error", "severity": "catastrophic"}
    )
    assert resp.status_code == 422
    assert captured == []


def test_rejects_non_boolean_has_digest(client, monkeypatch):
    """A free-text string smuggled into what should be a boolean field is
    rejected by Pydantic's own type validation, never coerced to a string
    and logged."""
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post(
        "/api/client-events",
        json={"event": "render_error", "hasDigest": "Authorization: Basic YWxpY2U="},
    )
    assert resp.status_code == 422
    assert captured == []


def test_overlong_code_value_is_rejected_outright(client, monkeypatch):
    """A `code` value longer than the field's max_length is rejected before
    normalization ever runs -- checked separately from the length-bounded
    hostile corpus below, since Pydantic's `max_length` constraint runs
    before the `@field_validator` that would otherwise normalize an
    unrecognized value to "unknown"."""
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post(
        "/api/client-events", json={"event": "render_error", "code": "x" * 5000}
    )
    assert resp.status_code == 422
    assert captured == []


@pytest.mark.parametrize("hostile", _HOSTILE_STRINGS)
def test_unknown_extra_field_with_hostile_content_is_rejected_and_never_emitted(
    client, monkeypatch, hostile
):
    """Regression for the fail-open regex-bypass findings across several
    review rounds: instead of trying to pattern-match hostile content out of
    a free-text field, the field itself no longer exists. Any extra field --
    whatever it's named, however hostile or long its value -- is rejected
    outright by `ClientEventReport`'s `extra="forbid"` before request
    handling begins, proven here by feeding the identical adversarial corpus
    that used to defeat the regex sanitizer, this time as an *illegal*
    field."""
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post(
        "/api/client-events", json={"event": "render_error", "message": hostile}
    )
    assert resp.status_code == 422
    assert captured == []


def test_deeply_nested_extra_field_is_rejected_regardless_of_internal_structure(
    client, monkeypatch
):
    """Proves the forbid-extra guarantee doesn't depend on inspecting what's
    *inside* an illegal field: an arbitrarily deep/nested credential-shaped
    object under an extra key is rejected purely because the key itself
    isn't part of the schema -- Pydantic never has to recurse into (or
    pattern-match against) its contents, unlike the old regex sanitizer."""
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post(
        "/api/client-events",
        json={
            "event": "render_error",
            "authDetails": {
                "Authorization": {
                    "scheme": "Basic",
                    "credential": "YWxpY2U6cGFzc3dvcmQ=",
                    "nested": {"again": ["Bearer", "z" * 60]},
                }
            },
        },
    )
    assert resp.status_code == 422
    assert captured == []


@pytest.mark.parametrize("hostile", _HOSTILE_STRINGS_WITHIN_CODE_LENGTH)
def test_hostile_code_value_always_normalizes_to_unknown(client, monkeypatch, hostile):
    """`code` is the one remaining string field, but it's a closed allowlist:
    anything not an exact match becomes "unknown" regardless of encoding,
    nesting, or control characters -- never forwarded verbatim to
    `emit_custom_event`/the logger."""
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post("/api/client-events", json={"event": "render_error", "code": hostile})
    assert resp.status_code == 202
    assert captured[0][1]["code"] == "unknown"


def test_per_user_rate_limit_drops_without_erroring(client, monkeypatch):
    import ai4ia_api.routers.client_events as mod

    monkeypatch.setattr(mod, "_hits", {})
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(mod, "emit_custom_event", lambda name, attrs: captured.append((name, attrs)))
    headers = {"X-Dev-User": "flooder"}
    for _ in range(mod._RATE_LIMIT_PER_MINUTE):
        resp = client.post("/api/client-events", json={"event": "window_error"}, headers=headers)
        assert resp.status_code == 202
    assert len(captured) == mod._RATE_LIMIT_PER_MINUTE

    # One more within the same window is dropped silently, not an error.
    over = client.post("/api/client-events", json={"event": "window_error"}, headers=headers)
    assert over.status_code == 202
    assert len(captured) == mod._RATE_LIMIT_PER_MINUTE

    # A different user is unaffected by another user's window.
    other = client.post(
        "/api/client-events", json={"event": "window_error"}, headers={"X-Dev-User": "someone-else"}
    )
    assert other.status_code == 202
    assert len(captured) == mod._RATE_LIMIT_PER_MINUTE + 1


def test_sweep_stale_users_evicts_only_inactive_entries():
    """Regression for MEDIUM-2: the per-call prune in `_rate_limited` only
    removed timestamps *inside* a user's own window deque -- it never removed
    the dict entry itself, so a long-running process accumulated one entry
    per distinct user ever seen, forever, even once every one of them had
    been inactive for hours. Exercises `_sweep_stale_users` directly with
    hand-built windows so "stale" vs "active" is deterministic rather than
    depending on wall-clock timing."""
    import ai4ia_api.routers.client_events as mod

    now = 1_000_000.0
    mod._hits.clear()
    mod._hits.update(
        {
            "stale-boundary": deque([now - mod._RATE_WINDOW_SECONDS - 0.01]),
            "stale-old": deque([now - mod._RATE_WINDOW_SECONDS - 500]),
            "active": deque([now - 1.0]),
            "empty-window": deque(),
        }
    )
    try:
        mod._sweep_stale_users(now)
        assert set(mod._hits) == {"active"}
        assert mod._last_sweep == now
    finally:
        mod._hits.clear()


def test_rate_limited_enforces_hard_cap_on_every_insertion(monkeypatch):
    """Regression for MEDIUM-1: the old design only enforced
    `_MAX_TRACKED_USERS` inside the periodic sweep, so a burst of distinct
    users arriving between sweeps could transiently exceed the cap before the
    next sweep ran. The fix enforces the cap on every insertion in
    `_rate_limited` itself. Uses a frozen (near-frozen) clock so every one of
    the ten users is still "active" when the next arrives -- proving the
    eviction is driven by the insertion-time cap, not by staleness."""
    import ai4ia_api.routers.client_events as mod

    monkeypatch.setattr(mod, "_hits", {})
    monkeypatch.setattr(mod, "_MAX_TRACKED_USERS", 3)
    monkeypatch.setattr(mod, "_last_sweep", 0.0)
    # A sweep interval far longer than the whole test keeps the periodic
    # sweep from ever firing, isolating this test to the insert-time cap path.
    monkeypatch.setattr(mod, "_SWEEP_INTERVAL_SECONDS", 1_000.0)
    clock = {"now": 0.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])

    for i in range(10):
        clock["now"] += 0.01  # negligible elapsed time -- nobody goes stale
        mod._rate_limited(f"user-{i}")
        # The cap must hold after *every single* insertion, not just once at
        # the end -- this is the crux of "enforce cap on every insertion".
        assert len(mod._hits) <= 3

    # LRU eviction: the three most-recently-inserted users survive.
    assert set(mod._hits) == {"user-7", "user-8", "user-9"}


def test_concurrent_sweep_and_insert_does_not_corrupt_state(monkeypatch):
    """Regression for MEDIUM-1's other half: `_sweep_stale_users` iterates
    `_hits.items()` while a concurrent insert from another thread mutates the
    same dict -- without a shared lock this either raises `RuntimeError:
    dictionary changed size during iteration` or silently corrupts state.
    Drives a dedicated "sweeper" thread calling `_sweep_stale_users` in a
    tight loop alongside several "inserter" threads concurrently calling
    `_rate_limited` with distinct, never-repeating user ids, so the sweeper
    is racing against genuine dict mutation for the whole test."""
    import ai4ia_api.routers.client_events as mod

    monkeypatch.setattr(mod, "_hits", {})
    monkeypatch.setattr(mod, "_last_sweep", 0.0)
    monkeypatch.setattr(mod, "_MAX_TRACKED_USERS", 1_000)
    errors: list[Exception] = []
    stop = threading.Event()

    def _sweeper() -> None:
        try:
            while not stop.is_set():
                mod._sweep_stale_users(mod.time.monotonic())
        except Exception as exc:  # pragma: no cover - assertion below also fails
            errors.append(exc)

    def _inserter(worker_id: int) -> None:
        try:
            for i in range(300):
                mod._rate_limited(f"sweep-race-{worker_id}-{i}")
        except Exception as exc:  # pragma: no cover - assertion below also fails
            errors.append(exc)

    sweeper_thread = threading.Thread(target=_sweeper)
    sweeper_thread.start()
    inserters = [threading.Thread(target=_inserter, args=(w,)) for w in range(6)]
    for t in inserters:
        t.start()
    for t in inserters:
        t.join(timeout=10)
    stop.set()
    sweeper_thread.join(timeout=10)

    assert errors == []
    assert not sweeper_thread.is_alive()
    assert all(not t.is_alive() for t in inserters)


def test_rate_limited_periodic_sweep_bounds_hit_dict_across_many_users(monkeypatch):
    """Integration-level companion to the two sweep tests above: drives the
    real caller (`_rate_limited`) across many distinct, one-shot users with a
    controllable clock and asserts `_hits` stays capped instead of growing
    linearly with the number of distinct users ever seen."""
    import ai4ia_api.routers.client_events as mod

    monkeypatch.setattr(mod, "_hits", {})
    monkeypatch.setattr(mod, "_last_sweep", 0.0)
    monkeypatch.setattr(mod, "_SWEEP_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr(mod, "_RATE_WINDOW_SECONDS", 1.0)

    clock = {"now": 0.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])

    for i in range(500):
        clock["now"] += 2.0  # past both the rate window and the sweep interval
        mod._rate_limited(f"user-{i}")

    # Every prior user is stale by the time the next call's sweep fires, so
    # the dict never accumulates more than the current caller's own entry.
    assert len(mod._hits) <= 2


def test_rate_limited_is_safe_under_concurrent_calls(monkeypatch):
    """Concurrency check for MEDIUM-2's fix: `_rate_limited` has no `await`
    point so it's atomic within a single asyncio worker by construction, but
    the CPython GIL only guarantees individual bytecode-level atomicity, not
    an entire read-modify-write dict+deque sequence. Drives it from real OS
    threads with overlapping user ids to prove the eviction/cap logic doesn't
    corrupt shared state or raise under genuinely concurrent access."""
    import ai4ia_api.routers.client_events as mod

    monkeypatch.setattr(mod, "_hits", {})
    monkeypatch.setattr(mod, "_MAX_TRACKED_USERS", 50)
    monkeypatch.setattr(mod, "_last_sweep", 0.0)
    errors: list[Exception] = []

    def _worker(worker_id: int) -> None:
        try:
            for i in range(200):
                mod._rate_limited(f"user-{worker_id}-{i % 5}")
        except Exception as exc:  # pragma: no cover - assertion below also fails
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert all(not t.is_alive() for t in threads)
    assert len(mod._hits) <= mod._MAX_TRACKED_USERS


def test_rate_limit_resets_after_window_elapses(client, monkeypatch):
    import ai4ia_api.routers.client_events as mod

    monkeypatch.setattr(mod, "_hits", {})
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(mod, "emit_custom_event", lambda name, attrs: captured.append((name, attrs)))
    headers = {"X-Dev-User": "flooder2"}
    for _ in range(mod._RATE_LIMIT_PER_MINUTE):
        client.post("/api/client-events", json={"event": "window_error"}, headers=headers)
    assert len(captured) == mod._RATE_LIMIT_PER_MINUTE

    # Simulate the window elapsing by rewinding the recorded hit timestamps.
    for uid, window in mod._hits.items():
        for i in range(len(window)):
            window[i] -= mod._RATE_WINDOW_SECONDS + 1

    resp = client.post("/api/client-events", json={"event": "window_error"}, headers=headers)
    assert resp.status_code == 202
    assert len(captured) == mod._RATE_LIMIT_PER_MINUTE + 1


def test_report_lands_in_real_logger_without_keyerror(client, monkeypatch):
    """Regression test for the original reserved-LogRecord-attribute bug
    (HIGH-1): `Logger.makeRecord` raises `KeyError` if `extra` contains a
    "message" (or "asctime") key, since those collide with attributes every
    LogRecord already has. `client_events.py` used to pass the report's raw
    text under a "message"-shaped key straight into `emit_custom_event`'s
    `attributes` dict, so every report with non-empty text raised that
    KeyError -- silently swallowed by `emit_custom_event`'s blanket
    except-pass, meaning the event never reached Application Insights even
    though this endpoint still returned 202.

    That specific collision is now structurally impossible (there is no
    free-text field left that could collide with a reserved attribute), but
    this test remains valuable as a real-logger-path regression guard for the
    endpoint as a whole, and additionally proves a hostile `code` value
    normalizes to "unknown" all the way down to the actual LogRecord, not
    just in a mocked capture list. Exercises the REAL logger (no mocked
    `emit_custom_event`) so a future regression can't hide behind a mock
    again."""
    monkeypatch.setattr(logging_setup, "_telemetry_configured", True)
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logging_setup._telemetry_logger.addHandler(handler)
    try:
        resp = client.post(
            "/api/client-events",
            json={
                "event": "window_error",
                "code": "Authorization: Basic YWxpY2U=",  # hostile, still <=40 chars
                "severity": "warning",
                "hasDigest": True,
            },
            headers={"X-Dev-User": "carol"},
        )
    finally:
        logging_setup._telemetry_logger.removeHandler(handler)

    assert resp.status_code == 202
    # The crux of the original regression: previously this list stayed empty
    # because the KeyError was raised (and swallowed) before the record was
    # emitted.
    assert len(records) == 1
    rec = records[0]
    assert getattr(rec, "microsoft.custom_event.name") == "client_event"
    assert rec.event == "window_error"
    assert rec.code == "unknown"  # hostile input, normalized -- never logged verbatim
    assert rec.severity == "warning"
    assert rec.hasDigest is True
    assert rec.userId
    # None of the old free-text attributes exist on the record at all.
    assert not hasattr(rec, "clientMessage")
    assert not hasattr(rec, "route")
    assert not hasattr(rec, "component")


def test_code_passes_through_when_known_and_normalizes_when_not(client, monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    client.post("/api/client-events", json={"event": "window_error", "code": "NotAllowedError"})
    client.post("/api/client-events", json={"event": "window_error", "code": "made_up_code"})
    client.post("/api/client-events", json={"event": "window_error"})

    codes = [attrs["code"] for _, attrs in captured]
    assert codes == ["NotAllowedError", "unknown", "unknown"]
