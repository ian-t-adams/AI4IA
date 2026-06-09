"""Voice Live (Phase 10) WebSocket relay contract + governance.

Exercises the ``/api/voice/live`` route end to end with a fake upstream socket
(no network): the disabled-by-default refusal, Origin/auth/entitlement denials,
and the happy-path bidirectional pump + per-session metering. The upstream
connector is swapped on ``app.state`` exactly like the REST voice tests swap the
gateway.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ai4ia_api.main import create_app
from ai4ia_api.routers.realtime import (
    DEV_SUBPROTOCOL,
    UpstreamMessage,
)
from tests.conftest import make_settings

ADMIN = {"X-Dev-User": "alice"}


def _origin(value: str = "http://localhost:3000") -> dict[str, str]:
    # A fresh dict per call: Starlette's ``websocket_connect`` mutates the passed
    # headers (``setdefault('sec-websocket-protocol', ...)``), so a shared dict
    # would leak one test's subprotocols into the next.
    return {"origin": value}


class FakeUpstream:
    """In-memory echo socket: every client frame comes back as ``echo:<frame>``."""

    def __init__(self) -> None:
        import asyncio

        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed = False
        self._queue: asyncio.Queue[UpstreamMessage] = asyncio.Queue()

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)
        await self._queue.put(UpstreamMessage("text", text=f"echo:{data}"))

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)
        await self._queue.put(UpstreamMessage("binary", data=b"echo:" + data))

    async def receive(self) -> UpstreamMessage:
        return await self._queue.get()

    async def close(self) -> None:
        self.closed = True


class FakeRealtimeConnector:
    """Injectable connector capturing connect args; optionally fails to connect."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.upstream = FakeUpstream()
        self.connects: list[dict] = []

    @asynccontextmanager
    async def connect(self, *, url: str, headers: dict[str, str], timeout: float):
        self.connects.append({"url": url, "headers": headers, "timeout": timeout})
        if self.fail:
            raise RuntimeError("upstream unreachable")
        try:
            yield self.upstream
        finally:
            await self.upstream.close()


def _client(**overrides) -> TestClient:
    settings = make_settings(admin_subjects="alice", **overrides)
    c = TestClient(app := create_app(settings))
    c.__enter__()
    c.app.state.realtime_connector = FakeRealtimeConnector()
    assert app is c.app
    return c


@pytest.fixture
def client():
    c = _client(realtime_enabled=True)
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _internal_id(client, headers) -> str:
    return client.get("/api/entitlement", headers=headers).json()["userId"]


# --------------------------------------------------------------------------- #
# Disabled by default (zero-regression posture).
# --------------------------------------------------------------------------- #


def test_live_disabled_by_default_refuses():
    # No realtime_enabled override -> defaults OFF -> route refuses before accept.
    c = _client()
    try:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(
                "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "u"], headers=_origin()
            ):
                pass
    finally:
        c.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# Auth / Origin / entitlement denials.
# --------------------------------------------------------------------------- #


def test_live_missing_auth_subprotocol_refused(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/voice/live", headers=_origin()):
            pass


def test_live_origin_rejected_when_allowlist_set():
    c = _client(realtime_enabled=True, realtime_allowed_origins="https://good.example")
    try:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(
                "/api/voice/live",
                subprotocols=[DEV_SUBPROTOCOL, "u"],
                headers={"origin": "https://evil.example"},
            ):
                pass
    finally:
        c.__exit__(None, None, None)


def test_live_disabled_user_refused(client):
    headers = {"X-Dev-User": "banned"}
    uid = _internal_id(client, headers)
    client.put(f"/api/admin/entitlements/{uid}", json={"disabled": True}, headers=ADMIN)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "banned"], headers=_origin()
        ):
            pass


def test_live_upstream_failure_closes(client):
    client.app.state.realtime_connector = FakeRealtimeConnector(fail=True)
    with client.websocket_connect(
        "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "u"], headers=_origin()
    ) as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


# --------------------------------------------------------------------------- #
# Happy path: bidirectional pump + governance side effects.
# --------------------------------------------------------------------------- #


def test_live_relay_pumps_both_directions(client):
    connector = client.app.state.realtime_connector
    with client.websocket_connect(
        "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "liveuser"], headers=_origin()
    ) as ws:
        ws.send_text('{"type":"session.update"}')
        assert ws.receive_text() == 'echo:{"type":"session.update"}'
        ws.send_bytes(b"\x01\x02pcm")
        assert ws.receive_bytes() == b"echo:\x01\x02pcm"

    # Upstream was opened exactly once with the gateway-derived URL + credential.
    assert len(connector.connects) == 1
    opened = connector.connects[0]
    assert opened["url"].startswith("ws://gateway.test/realtime")
    assert "deployment=" in opened["url"]
    assert connector.upstream.sent_text == ['{"type":"session.update"}']
    assert connector.upstream.sent_bytes == [b"\x01\x02pcm"]
    assert connector.upstream.closed is True


def test_live_session_is_metered(client):
    headers = {"X-Dev-User": "meterlive"}
    with client.websocket_connect(
        "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "meterlive"], headers=_origin()
    ) as ws:
        ws.send_text("ping")
        ws.receive_text()

    summary = client.get("/api/usage", headers=headers).json()
    assert summary["totalRequests"] >= 1


def test_live_accepts_matching_origin_with_allowlist():
    c = _client(realtime_enabled=True, realtime_allowed_origins="https://good.example")
    try:
        with c.websocket_connect(
            "/api/voice/live",
            subprotocols=[DEV_SUBPROTOCOL, "u"],
            headers={"origin": "https://good.example"},
        ) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "echo:hello"
    finally:
        c.__exit__(None, None, None)

