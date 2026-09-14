"""Signed actor policy must select the real setup-only relay before provider egress."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from urllib.parse import urlsplit

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ai4ia_api.main import create_app
from ai4ia_api.hard_quota.models import Surface
from ai4ia_api.realtime_canary import SETUP_INPUT
from ai4ia_api.routers.realtime import UpstreamMessage
from tests.conftest import make_settings
from tests.test_auth_entra import BARE_GUID, ISSUER, KID, TENANT, _new_keypair, _provider
from tests.test_realtime_staged_api import GA_SETTINGS

ACTOR = "00000000-0000-0000-0000-000000000003"
MONITOR = "00000000-0000-0000-0000-000000000001"
ORIGIN = "https://web.example.test"


class SetupUpstream:
    def __init__(self):
        self.sent_text = []
        self.sent_bytes = []
        self.closed = False
        self.messages = asyncio.Queue()
        self.messages.put_nowait(UpstreamMessage("text", text='{"type":"session.created"}'))

    async def send_text(self, text):
        self.sent_text.append(json.loads(text))
        await self.messages.put(UpstreamMessage("text", text='{"type":"session.updated"}'))

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def receive(self):
        return await self.messages.get()

    async def close(self):
        self.closed = True


class SetupConnector:
    def __init__(self):
        self.calls = []
        self.upstream = SetupUpstream()

    @asynccontextmanager
    async def connect(self, **kwargs):
        self.calls.append(kwargs)
        try:
            yield self.upstream
        finally:
            await self.upstream.close()


@pytest.fixture
def setup_client():
    config = {
        "realtimeCanaryActor": {"tenantId": TENANT, "subject": ACTOR},
        "canaryActor": {"tenantId": TENANT, "subject": MONITOR},
        "domains": {
            "models": {"default": {"allow": ["realtime"]}},
            "tools": {"default": {"allow": []}},
            "documents": {"default": {"allow": []}},
        },
        "spend": {"default": {"requestsPerMinute": 50}},
    }
    settings = make_settings(
        auth_provider="entra", entra_tenant_id=TENANT, entra_audience=BARE_GUID,
        group_policy_enabled=True, group_policy_json=json.dumps(config),
        realtime_enabled=True, realtime_protocol="ga", realtime_allowed_origins=ORIGIN,
        realtime_base_url="https://realtime-gateway.test/openai",
        realtime_gateway_api_key="synthetic-preview-key",
        **GA_SETTINGS,
    )
    private, jwks = _new_keypair(KID)

    def token(subject=ACTOR):
        now = int(time.time())
        return jwt.encode({
            "aud": BARE_GUID, "iss": ISSUER, "tid": TENANT, "oid": subject,
            "iat": now, "exp": now + 3600, "roles": [],
        }, private, algorithm="RS256", headers={"kid": KID})

    app = create_app(settings)
    with TestClient(app) as client:
        app.state.auth_provider = _provider(jwks, audience=BARE_GUID)
        app.state.realtime_connector = connector = SetupConnector()
        yield client, token, connector, config


def setup_target(client, token):
    response = client.get(
        "/api/canary/realtime-capabilities", headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    capability = response.json()
    assert capability["ready"] is True
    return (
        "/api/voice/live?provider=azure_openai"
        f"&model={capability['model']}&region={capability['region']}"
    )


def test_real_ga_actor_can_only_complete_one_setup_exchange(setup_client):
    client, token, connector, _ = setup_client
    bearer = token()
    target = setup_target(client, bearer)
    with client.websocket_connect(
        target, subprotocols=["ai4ia-bearer", bearer], headers={"origin": ORIGIN},
    ) as ws:
        assert dict(ws.extra_headers)[b"x-ai4ia-realtime-protocol"] == b"ga"
        assert json.loads(ws.receive_text())["type"] == "session.created"
        ws.send_text(SETUP_INPUT)
        assert json.loads(ws.receive_text())["type"] == "session.updated"
    assert len(connector.calls) == 1
    assert len(connector.upstream.sent_text) == 1
    session = connector.upstream.sent_text[0]["session"]
    assert session["type"] == "realtime"
    assert session["output_modalities"] == ["text"]
    assert session["audio"]["input"]["turn_detection"] is None
    assert not session.get("tools")
    assert connector.upstream.sent_bytes == []
    assert connector.upstream.closed


@pytest.mark.parametrize("payload", [
    '{"type":"response.create"}',
    '{"type":"input_audio_buffer.append","audio":"AAAA"}',
    '{"type":"session.update","session":{"instructions":"unapproved"}}',
    b"audio",
])
def test_setup_actor_cannot_send_audio_response_or_context(setup_client, payload):
    client, token, connector, _ = setup_client
    bearer = token()
    with client.websocket_connect(
        setup_target(client, bearer), subprotocols=["ai4ia-bearer", bearer],
        headers={"origin": ORIGIN},
    ) as ws:
        assert json.loads(ws.receive_text())["type"] == "session.created"
        if isinstance(payload, bytes):
            ws.send_bytes(payload)
        else:
            ws.send_text(payload)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert connector.upstream.sent_text == []
    assert connector.upstream.sent_bytes == []
    assert connector.upstream.closed


@pytest.mark.parametrize("change", ["preview", "tools", "session", "agent", "monitor", "missing_guard", "policy_disabled"])
def test_setup_prerequisites_refuse_before_provider_open(setup_client, change):
    client, token, connector, _ = setup_client
    bearer = token(MONITOR if change == "monitor" else ACTOR)
    allowed = token()
    target = setup_target(client, allowed)
    if change == "preview":
        from ai4ia_api.realtime_protocol import RealtimeProtocol
        client.app.state.settings.realtime_protocol = RealtimeProtocol.preview
    elif change in ("tools", "agent", "session"):
        target += f"&{change}=unapproved"
    elif change == "missing_guard":
        client.app.state.realtime_canary_dispatch_guard = None
    elif change == "policy_disabled":
        client.app.state.settings.group_policy_enabled = False
    # A caller label cannot change which actor policy was selected.
    target += "&profile=realtime-setup-canary"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            target, subprotocols=["ai4ia-bearer", bearer], headers={"origin": ORIGIN},
        ) as ws:
            ws.send_text(SETUP_INPUT)
            ws.receive_text()
    assert connector.calls == []


def test_setup_rechecks_actor_mapping_before_forwarding_the_update(setup_client):
    client, token, connector, config = setup_client
    bearer = token()
    with client.websocket_connect(
        setup_target(client, bearer), subprotocols=["ai4ia-bearer", bearer], headers={"origin": ORIGIN},
    ) as ws:
        assert json.loads(ws.receive_text())["type"] == "session.created"
        config.pop("realtimeCanaryActor")
        client.app.state.settings.group_policy_json = json.dumps(config)
        ws.send_text(SETUP_INPUT)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert connector.upstream.sent_text == []
    assert connector.upstream.closed


@pytest.mark.parametrize("surface", [item for item in get_args(Surface) if item != "realtime"])
@pytest.mark.parametrize("final", [False, True])
async def test_setup_actor_cannot_use_other_metered_surfaces(setup_client, surface, final):
    from ai4ia_api.auth.base import AuthCredentials
    from ai4ia_api.policy.context import bind_authenticated, clear_policy_context
    from ai4ia_api.policy.dispatch import authorize_dispatch
    from ai4ia_api.policy.models import PolicyError

    client, token, connector, _ = setup_client
    user = await client.app.state.auth_provider.authenticate(AuthCredentials(token=token()))
    bind_authenticated(client.app.state.policy, user)
    entry = next(model for model in client.app.state.catalog.models if model.category == "realtime")
    deployment = entry.options[0].deploymentName
    try:
        with pytest.raises(PolicyError, match="canary_policy_incompatible"):
            await authorize_dispatch(
                surface, deployment=deployment, payload={}, final=final,
                service=client.app.state.policy, expected_owner=user.internal_user_id,
            )
        # The identical actor/model is eligible at read-only realtime admission;
        # final dispatch still needs the real, request-local setup proof.
        await authorize_dispatch(
            "realtime", deployment=deployment, payload={}, final=False,
            service=client.app.state.policy, expected_owner=user.internal_user_id,
        )
        with pytest.raises(PolicyError):
            await authorize_dispatch(
                "realtime", deployment=deployment, payload={}, final=True,
                service=client.app.state.policy, expected_owner=user.internal_user_id,
            )
    finally:
        clear_policy_context()
    assert connector.calls == []


@pytest.mark.parametrize("other", ["canaryActor", "evaluationActor"])
def test_operator_actor_markers_must_be_distinct(other):
    from ai4ia_api.policy.models import parse_policy_config

    marker = {"tenantId": TENANT, "subject": ACTOR}
    with pytest.raises(ValueError, match="must be distinct"):
        parse_policy_config(json.dumps({"realtimeCanaryActor": marker, other: marker}))
    config = parse_policy_config(json.dumps({"realtimeCanaryActor": marker}))
    assert config.canaryActor is None and config.evaluationActor is None


def test_paused_policy_at_startup_preserves_restricted_actor_identity(setup_client):
    client, token, _connector, _ = setup_client
    settings = client.app.state.settings.model_copy(update={"group_policy_enabled": False})
    app = create_app(settings)
    with TestClient(app) as paused:
        paused.app.state.auth_provider = client.app.state.auth_provider
        paused.app.state.realtime_connector = connector = SetupConnector()
        bearer = token()
        capability = paused.get(
            "/api/canary/realtime-capabilities", headers={"Authorization": f"Bearer {bearer}"},
        )
        assert capability.json()["ready"] is False
        headers = {"Authorization": f"Bearer {bearer}"}
        session = paused.post("/api/sessions", json={"title": "Synthetic fixture"}, headers=headers).json()
        documents = f"/api/sessions/{session['id']}/documents"
        assert paused.post(
            documents, files={"file": ("fixture.txt", b"synthetic", "text/plain")}, headers=headers,
        ).status_code == 503
        assert paused.get(documents, headers=headers).status_code == 503
        with pytest.raises(WebSocketDisconnect):
            with paused.websocket_connect(
                "/api/voice/live?provider=azure_openai",
                subprotocols=["ai4ia-bearer", bearer], headers={"origin": ORIGIN},
            ) as ws:
                ws.receive_text()
        assert connector.calls == []


@pytest.mark.parametrize("enabled", [True, False])
async def test_pausing_policy_does_not_restore_document_read_or_write_and_cleanup_stays_allowed(
    setup_client, enabled,
):
    from ai4ia_api.sessions.models import Document

    client, token, _connector, _ = setup_client
    client.app.state.settings.group_policy_enabled = enabled
    headers = {"Authorization": f"Bearer {token()}"}
    session = client.post("/api/sessions", json={"title": "Synthetic fixture"}, headers=headers).json()
    path = f"/api/sessions/{session['id']}/documents"
    uploaded = client.post(path, files={"file": ("fixture.txt", b"synthetic fixture", "text/plain")}, headers=headers)
    assert uploaded.status_code in (403, 503), uploaded.text
    assert client.get(path, headers=headers).status_code in (403, 503)
    document = Document(
        userId=session["userId"], sessionId=session["id"], filename="owned-cleanup.txt", text="synthetic",
    )
    await client.app.state.session_repo.add_document(session["userId"], document)
    assert client.delete(f"{path}/{document.id}", headers=headers).status_code == 204
    assert client.get(f"/api/sessions/{session['id']}", headers=headers).status_code == 200


def test_unmarked_ordinary_actor_keeps_flag_off_document_behavior(setup_client):
    client, token, _connector, _ = setup_client
    client.app.state.settings.group_policy_enabled = False
    headers = {"Authorization": f"Bearer {token('00000000-0000-0000-0000-000000000099')}"}
    session = client.post("/api/sessions", json={"title": "Synthetic fixture"}, headers=headers).json()
    path = f"/api/sessions/{session['id']}/documents"
    uploaded = client.post(path, files={"file": ("fixture.txt", b"synthetic fixture", "text/plain")}, headers=headers)
    assert uploaded.status_code == 201, uploaded.text
    assert len(client.get(path, headers=headers).json()) == 1


def test_setup_lifetime_includes_slow_connection_and_still_closes_it(setup_client, monkeypatch):
    from ai4ia_api.realtime_canary import RealtimeSetup
    from ai4ia_api.routers import realtime as relay_module

    class ShortSetup(RealtimeSetup):
        def __post_init__(self):
            super().__post_init__()
            self.deadline = self.clock() + 0.5

    monkeypatch.setattr(relay_module, "RealtimeSetup", ShortSetup)
    client, token, _connector, _ = setup_client

    class SlowConnector(SetupConnector):
        reached_handshake = False

        @asynccontextmanager
        async def connect(self, **kwargs):
            self.calls.append(kwargs)
            try:
                await asyncio.sleep(1)
                self.reached_handshake = True
                yield self.upstream
            finally:
                await self.upstream.close()

    client.app.state.realtime_connector = slow = SlowConnector()
    bearer = token()
    with client.websocket_connect(
        setup_target(client, bearer), subprotocols=["ai4ia-bearer", bearer], headers={"origin": ORIGIN},
    ) as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert len(slow.calls) == 1
    assert not slow.reached_handshake
    assert slow.upstream.closed


async def test_workflow_realtime_probe_crosses_real_signed_policy_and_relay(setup_client, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root))
    import aiohttp
    from scripts.canaries.configuration import Configuration
    from scripts.canaries.contracts import Report, Run, stamp
    from scripts.canaries.monitor import realtime
    from scripts.canaries.transport import Response

    client, token, connector, _ = setup_client

    class ApplicationTransport:
        handshake_protocol = None

        async def request(self, method, url, *, token=None, **_):
            parsed = urlsplit(url)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            result = client.request(
                method, parsed.path + (f"?{parsed.query}" if parsed.query else ""),
                headers=headers, follow_redirects=False,
            )
            return Response(
                result.status_code, result.content, 0.01,
                result.headers.get("content-type", "").split(";", 1)[0],
            )

        def client(self, _url, *, websocket):
            assert websocket
            return self

        @asynccontextmanager
        async def ws_connect(self, url, *, protocols, origin, **_):
            parsed = urlsplit(url)
            with client.websocket_connect(
                parsed.path + "?" + parsed.query, subprotocols=list(protocols),
                headers={"origin": origin},
            ) as ws:
                self.handshake_protocol = dict(ws.extra_headers)[b"x-ai4ia-realtime-protocol"].decode()

                class Socket:
                    protocol = ws.accepted_subprotocol

                    async def send_str(self, text):
                        ws.send_text(text)

                    async def close(self, **_):
                        ws.close()

                    def __aiter__(self):
                        async def events():
                            while True:
                                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=ws.receive_text())
                        return events()

                yield Socket()

    now = datetime.now(timezone.utc)
    config = Configuration(
        TENANT, "22222222-2222-2222-2222-222222222222", ACTOR, f"api://{BARE_GUID}",
        ORIGIN, "https://api.example.test", "55555555-5555-5555-5555-555555555555",
        stamp(now + timedelta(hours=1)), 4, 21600, True, True, True, True,
    )
    report = Report(Run("owner/repo", 1, 200, 2, 1, "a" * 40), stamp(now))
    report.mark("posture", "pass")
    source = json.loads((root / "infra" / "models.json").read_text(encoding="utf-8"))
    await realtime(ApplicationTransport(), config, token(), source, {"models": []}, report)
    assert report.stages["realtime"].outcome == "pass", report.document()
    assert len(connector.calls) == 1
    assert len(connector.upstream.sent_text) == 1
    assert not connector.upstream.sent_bytes
