"""Client-events beacon: auth gating, validation bounds, rate limiting, and
the ``emit_custom_event`` bridge (mirrors ``test_telemetry.py``'s style for
asserting the customEvents contract)."""
from __future__ import annotations

from ai4ia_api.auth.base import AuthError


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
    assert attrs["message"] == "Boom"
    assert attrs["route"] == "/"
    assert attrs["component"] == "ChatApp"
    assert attrs["userId"]


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
    assert attrs["message"] is None
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
