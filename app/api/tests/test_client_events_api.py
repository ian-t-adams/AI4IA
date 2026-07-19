"""Client-events beacon: auth gating, validation bounds, rate limiting, and
the ``emit_custom_event`` bridge (mirrors ``test_telemetry.py``'s style for
asserting the customEvents contract)."""
from __future__ import annotations

import logging
import threading
from collections import deque

from ai4ia_api import logging_setup
from ai4ia_api.auth.base import AuthError
from ai4ia_api.routers.client_events import _sanitize


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
        json={"event": "render_error", "message": "Boom", "route": "/", "component": "ChatApp"},
        headers={"X-Dev-User": "alice"},
    )
    assert resp.status_code == 202
    assert len(captured) == 1
    name, attrs = captured[0]
    assert name == "client_event"
    assert attrs["source"] == "browser"
    assert attrs["event"] == "render_error"
    # Deliberately not "message" -- see the real-logger-path regression test
    # below for why that key collides with a reserved LogRecord attribute.
    assert attrs["clientMessage"] == "Boom"
    assert attrs["route"] == "/"
    assert attrs["component"] == "ChatApp"
    assert attrs["userId"]
    assert attrs["code"] == "unknown"


def test_rejects_unknown_event_type(client, monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post("/api/client-events", json={"event": "made_up_event"})
    assert resp.status_code == 422
    assert captured == []


def test_rejects_oversized_message(client):
    resp = client.post(
        "/api/client-events",
        json={"event": "render_error", "message": "x" * 301},
    )
    assert resp.status_code == 422


def test_empty_optional_fields_become_none(client, monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post("/api/client-events", json={"event": "microphone_error"})
    assert resp.status_code == 202
    _, attrs = captured[0]
    assert attrs["clientMessage"] is None
    assert attrs["route"] is None
    assert attrs["component"] is None


def test_per_user_rate_limit_drops_without_erroring(client, monkeypatch):
    import ai4ia_api.routers.client_events as mod

    monkeypatch.setattr(mod, "_hits", {})
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(mod, "emit_custom_event", lambda name, attrs: captured.append((name, attrs)))
    headers = {"X-Dev-User": "flooder"}
    for _ in range(mod._RATE_LIMIT_PER_MINUTE):
        resp = client.post("/api/client-events", json={"event": "unhandled_error"}, headers=headers)
        assert resp.status_code == 202
    assert len(captured) == mod._RATE_LIMIT_PER_MINUTE

    # One more within the same window is dropped silently, not an error.
    over = client.post("/api/client-events", json={"event": "unhandled_error"}, headers=headers)
    assert over.status_code == 202
    assert len(captured) == mod._RATE_LIMIT_PER_MINUTE

    # A different user is unaffected by another user's window.
    other = client.post(
        "/api/client-events", json={"event": "unhandled_error"}, headers={"X-Dev-User": "someone-else"}
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
        client.post("/api/client-events", json={"event": "unhandled_error"}, headers=headers)
    assert len(captured) == mod._RATE_LIMIT_PER_MINUTE

    # Simulate the window elapsing by rewinding the recorded hit timestamps.
    for uid, window in mod._hits.items():
        for i in range(len(window)):
            window[i] -= mod._RATE_WINDOW_SECONDS + 1

    resp = client.post("/api/client-events", json={"event": "unhandled_error"}, headers=headers)
    assert resp.status_code == 202
    assert len(captured) == mod._RATE_LIMIT_PER_MINUTE + 1


def test_report_lands_in_real_logger_without_keyerror(client, monkeypatch):
    """Regression test for the reserved-LogRecord-attribute bug (HIGH-1):
    `Logger.makeRecord` raises `KeyError` if `extra` contains a "message" (or
    "asctime") key, since those collide with attributes every LogRecord
    already has. `client_events.py` used to pass the report's text under a
    "message" key straight into `emit_custom_event`'s `attributes` dict, so
    every report with non-empty text raised that KeyError -- silently
    swallowed by `emit_custom_event`'s blanket except-pass, meaning the event
    never reached Application Insights even though this endpoint still
    returned 202. This exercises the REAL logger (no mocked
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
                "event": "unhandled_error",
                "message": "Something broke",
                "route": "/chat",
                "component": "ChatApp",
                "code": "TypeError",
            },
            headers={"X-Dev-User": "carol"},
        )
    finally:
        logging_setup._telemetry_logger.removeHandler(handler)

    assert resp.status_code == 202
    # The crux of the regression: previously this list stayed empty because
    # the KeyError was raised (and swallowed) before the record was emitted.
    assert len(records) == 1
    rec = records[0]
    assert getattr(rec, "microsoft.custom_event.name") == "client_event"
    assert rec.clientMessage == "Something broke"
    assert rec.event == "unhandled_error"
    assert rec.route == "/chat"
    assert rec.component == "ChatApp"
    assert rec.code == "TypeError"


def test_code_passes_through_when_known_and_normalizes_when_not(client, monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    client.post("/api/client-events", json={"event": "unhandled_error", "code": "NotAllowedError"})
    client.post("/api/client-events", json={"event": "unhandled_error", "code": "made_up_code"})
    client.post("/api/client-events", json={"event": "unhandled_error"})

    codes = [attrs["code"] for _, attrs in captured]
    assert codes == ["NotAllowedError", "unknown", "unknown"]


def test_sanitize_redacts_hostile_message_content():
    # Mirrors clientTelemetry.test.ts's "redaction of hostile message
    # content" cases -- fixtures are intentionally low-entropy, repeated
    # characters, never realistic-looking secrets.
    long_opaque_run = "x" * 30
    short_key_value = "y" * 20
    assert (
        _sanitize(f"Authorization: {short_key_value} rejected")
        == "Authorization=[redacted] rejected"
    )
    assert (
        _sanitize("Failed to fetch https://example.test/path?a=1&b=2")
        == "Failed to fetch [redacted-url]"
    )
    assert (
        _sanitize("User jane.doe@example.test not found")
        == "User [redacted-email] not found"
    )
    assert (
        _sanitize("session 11111111-2222-3333-4444-555555555555 crashed")
        == "session [redacted-id] crashed"
    )
    assert (
        _sanitize(f"token {long_opaque_run} invalid") == "token [redacted-token] invalid"
    )


def test_sanitize_redacts_auth_scheme_word_together_with_its_credential():
    """Regression for the HIGH finding: the auth-header pattern's value group
    (``[^\\s"&,]+``) stops at the first whitespace, so before the fix it
    matched -- and redacted -- only the scheme word ("Basic"/"Bearer") while
    leaving the credential that followed it completely untouched in plain
    text. The fix wraps an optional scheme-word group around the value so the
    scheme and its credential are consumed as a single match. Covered for
    both schemes and both a short and a long (24+ char) credential, since the
    long case is also eligible for the separate generic long-opaque-token
    catch-all -- proving the auth-header pattern (which runs first) fully
    owns it rather than a later pattern happening to clean up what it
    missed."""
    fixtures = {
        "Basic": ("b" * 12, "c" * 40),
        "Bearer": ("d" * 12, "e" * 40),
    }
    for scheme, (short_cred, long_cred) in fixtures.items():
        for credential in (short_cred, long_cred):
            result = _sanitize(f"Authorization: {scheme} {credential} failed")
            assert result == "Authorization=[redacted] failed"
            assert scheme not in result
            assert credential not in result


def test_sanitize_redacts_json_serialized_authorization_key():
    """Regression for a follow-up acceptance round: a JSON-serialized key
    like ``{"Authorization":"Basic <cred>"}`` has a closing key-quote sitting
    directly between the label and the colon, which the prior fix's pattern
    didn't account for and so left completely unmatched."""
    cred = "f" * 16
    result = _sanitize('{"Authorization":"Basic ' + cred + '"} rejected')
    assert cred not in result
    assert "Basic" not in result
    assert "Authorization=[redacted]" in result


def test_sanitize_redacts_quoted_credential_after_unquoted_scheme():
    """Regression for a follow-up acceptance round: a quoted credential
    following an *unquoted* scheme word (``Authorization: Basic "<cred>"``)
    previously defeated the optional scheme-word group -- the old bare-token
    alternative excludes `"`, so backtracking gave up on matching the scheme
    word at all and redacted only "Basic", leaving the quoted credential
    fully exposed."""
    cred = "g" * 16
    result = _sanitize(f'Authorization: Basic "{cred}" failed')
    assert cred not in result
    assert "Basic" not in result
    assert result == "Authorization=[redacted] failed"


def test_sanitize_redacts_standalone_scheme_and_credential_with_no_label():
    """Regression for a follow-up acceptance round: bare scheme+credential
    with no "Authorization"/"token" label at all -- e.g. `Basic: <cred>` or a
    `Bearer` sitting in punctuation/JSON nesting -- previously passed through
    completely untouched, since `Basic`/`Digest`/`Negotiate`/`NTLM`/`OAuth`
    were never in the label-prefixed pattern's label list."""
    basic_cred = "h" * 12
    bearer_cred = "i" * 40
    result_basic = _sanitize(f"Basic: {basic_cred} was rejected")
    assert basic_cred not in result_basic
    assert "Basic:" not in result_basic

    result_bearer = _sanitize(f'request failed ("Bearer {bearer_cred}")')
    # Unlike the label-prefixed pattern (which drops the scheme word
    # entirely), the standalone pattern's replacement preserves the
    # matched scheme word -- only the credential value after it is
    # opaque -- so the correct assertion is that the scheme word is
    # redacted together with its credential, not that it vanishes.
    assert bearer_cred not in result_bearer
    assert result_bearer == 'request failed ("Bearer=[redacted]")'


def test_sanitize_leaves_trailing_scheme_word_alone_when_nothing_follows():
    # "Bearer" here has no colon/equals and nothing follows it (just a
    # trailing period), so there's no credential-shaped value for the
    # standalone pattern to consume.
    assert _sanitize("I love Bearer.") == "I love Bearer."


def test_sanitize_does_not_let_content_survive_for_userid_correlation():
    # The endpoint always tags the report with the authenticated user's id
    # (see `attrs["userId"]` above) -- proving the message is fully scrubbed
    # here is what keeps that user-tagged record from preserving hostile
    # content against them.
    long_opaque_run = "x" * 30
    result = _sanitize(f"for jane.doe@example.test, token={long_opaque_run}")
    assert "jane.doe@example.test" not in result
    assert long_opaque_run not in result


def test_report_endpoint_sanitizes_before_emitting(client, monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.client_events.emit_custom_event",
        lambda name, attrs: captured.append((name, attrs)),
    )
    resp = client.post(
        "/api/client-events",
        json={
            "event": "unhandled_error",
            "message": "User jane.doe@example.test not found",
            "route": "/x?token=" + "y" * 20,
        },
    )
    assert resp.status_code == 202
    _, attrs = captured[0]
    assert attrs["clientMessage"] == "User [redacted-email] not found"
    assert "jane.doe@example.test" not in attrs["route"]
