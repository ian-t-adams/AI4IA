"""Deletion must not erase quota or reopen governed realtime sessions."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app
from ai4ia_api.realtime_protocol import RealtimeProtocol
from ai4ia_api.routers.realtime import DEV_SUBPROTOCOL, UpstreamMessage
from tests.conftest import make_settings
from tests.test_hard_quota_dispatch import response_for
from tests.test_realtime_api import ScriptedRealtimeConnector, _client, _origin
from tests.test_realtime_staged_api import GA_SETTINGS


def test_deleting_a_conversation_preserves_quota_and_cannot_dispatch_a_new_turn():
    settings = make_settings(
        session_deletion_enabled=True, hard_quota_enabled=True,
        entitlements_enabled=False, admin_subjects="alice",
    )
    app = create_app(settings)
    sent = []
    with TestClient(app) as client:
        assert client.portal is not None
        http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: (sent.append(request) or response_for("chat", request)),
        ))
        try:
            app.state.gateway = ModelGatewayClient(settings, http)
            headers = {"X-Dev-User": "alice"}
            uid = client.get("/api/entitlement", headers=headers).json()["userId"]
            store = app.state.hard_quota.reservations.store
            store.seed(uid)
            model = next(model for model in app.state.catalog.conversational_models() if model.api == "chat")
            sid = client.post("/api/sessions", headers=headers, json={"model": model.id}).json()["id"]
            other = client.post("/api/sessions", headers=headers, json={"model": model.id}).json()["id"]
            assert client.post("/api/chat", headers=headers, json={
                "sessionId": sid, "content": "hello", "stream": False,
            }).status_code == 200
            assert len(sent) == 1
            state_before = client.portal.call(store.read, uid)
            assert len(state_before.state.entries) == 1
            assert client.put(
                f"/api/admin/entitlements/{uid}", headers=headers, json={"requestsPerMinute": 0},
            ).status_code == 200
            deletion = client.delete(f"/api/sessions/{sid}", headers=headers)
            assert deletion.status_code == 202 and deletion.json()["state"] == "pending"
            assert client.get(f"/api/sessions/{sid}/deletion", headers=headers).status_code == 200
            assert client.get(f"/api/sessions/{other}", headers=headers).status_code == 200
            assert client.portal.call(store.read, uid).state.entries == state_before.state.entries
            assert client.put(
                f"/api/admin/entitlements/{uid}", headers=headers, json={"requestsPerMinute": 2},
            ).status_code == 200
            assert client.post("/api/chat", headers=headers, json={
                "sessionId": sid, "content": "must not dispatch", "stream": False,
            }).status_code == 404
            assert len(sent) == 1
            assert client.post("/api/chat", headers=headers, json={
                "sessionId": other, "content": "hello", "stream": False,
            }).status_code == 200
            assert len(sent) == 2
            assert len(client.portal.call(store.read, uid).state.entries) == 2
        finally:
            client.portal.call(http.aclose)


@pytest.mark.parametrize("protocol", list(RealtimeProtocol))
def test_deleted_v1_session_cannot_reconnect_on_either_realtime_protocol(protocol):
    client = _client(
        realtime_enabled=True, hard_quota_enabled=True, entitlements_enabled=False,
        session_deletion_enabled=True, realtime_protocol=protocol, **GA_SETTINGS,
    )
    try:
        headers = {"X-Dev-User": "alice"}
        uid = client.get("/api/entitlement", headers=headers).json()["userId"]
        client.app.state.hard_quota.reservations.store.seed(uid)
        sid = client.post("/api/sessions", headers=headers, json={}).json()["id"]
        other = client.post("/api/sessions", headers=headers, json={}).json()["id"]
        connector = ScriptedRealtimeConnector([UpstreamMessage("close", close_code=1000)])
        client.app.state.realtime_connector = connector

        def connect(session_id):
            with client.websocket_connect(
                f"/api/voice/live?session={session_id}",
                subprotocols=[DEV_SUBPROTOCOL, "alice"], headers=_origin(),
            ) as websocket:
                assert websocket.receive()["type"] == "websocket.close"

        connect(sid)
        assert len(connector.connects) == 1
        assert client.delete(f"/api/sessions/{sid}", headers=headers).status_code == 202
        with pytest.raises(WebSocketDisconnect):
            connect(sid)
        assert len(connector.connects) == 1
        fresh = ScriptedRealtimeConnector([UpstreamMessage("close", close_code=1000)])
        client.app.state.realtime_connector = fresh
        connect(other)
        assert len(fresh.connects) == 1
        assert client.get(f"/api/sessions/{other}", headers=headers).status_code == 200
    finally:
        client.__exit__(None, None, None)
