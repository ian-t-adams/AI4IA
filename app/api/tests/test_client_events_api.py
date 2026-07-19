"""Client-events beacon: auth gating, validation bounds, rate limiting, and
the ``emit_custom_event`` bridge (mirrors ``test_telemetry.py``'s style for
asserting the customEvents contract)."""
from __future__ import annotations

import logging

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
