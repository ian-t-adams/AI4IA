"""A live audio frame needs fresh authorization, not repeated owner-store reads."""
from __future__ import annotations

import asyncio
import json
import socket
import time
from types import SimpleNamespace

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError

from ai4ia_api.catalog import load_catalog
from ai4ia_api.entitlements.memory_store import InMemoryEntitlementStore
from ai4ia_api.entitlements.models import Entitlement
from ai4ia_api.entitlements.service import EntitlementService
from ai4ia_api.policy.context import bind_authenticated, clear_policy_context, require_policy
from ai4ia_api.policy.dispatch import authorize_dispatch
from ai4ia_api.policy.models import PolicyError, PolicyRequest
from ai4ia_api.policy.service import PolicyService
from ai4ia_api.routers.realtime import _check_voice_frame_policy
from ai4ia_api.usage.models import WindowTotals
from tests.conftest import make_settings
from tests.test_entitlement_service import CountingReader
from tests.test_group_policy import TENANT, user


class FrameStore(InMemoryEntitlementStore):
    def __init__(self) -> None:
        super().__init__()
        self.strict_reads = 0
        self.delay = 0.0

    async def get_strict(self, user_id: str) -> Entitlement | None:
        self.strict_reads += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return await super().get_strict(user_id)


@pytest.fixture
def frame_policy():
    store = FrameStore()
    reader = CountingReader()
    config = {"domains": {"avatars": {"default": {"allow": ["use"]}}}}
    settings = make_settings(
        group_policy_enabled=True, group_policy_json=json.dumps(config),
        entra_tenant_id=TENANT, entra_allowed_tenants=TENANT,
    )
    policy = PolicyService(
        settings, catalog=load_catalog(),
        entitlements=EntitlementService(store, reader, Entitlement.unlimited()),
    )
    principal = user()
    bind_authenticated(policy, principal)
    yield policy, store, reader, principal, config
    clear_policy_context()


def speech_target():
    return SimpleNamespace(deployment=None, model_id="gpt-realtime")


async def test_avatar_frame_reads_current_owner_once_and_every_next_frame_is_fresh(frame_policy):
    policy, store, _, _, _ = frame_policy
    for frame in range(3):
        await _check_voice_frame_policy(speech_target(), policy, avatar=True)
        assert store.strict_reads == frame + 1

    await store.put(Entitlement(id="owner", userId="owner", disabled=True))
    with pytest.raises(PolicyError):
        await _check_voice_frame_policy(speech_target(), policy, avatar=True)
    assert store.strict_reads == 4


async def test_voice_only_frame_uses_the_same_fresh_single_read(frame_policy):
    policy, store, _, _, _ = frame_policy
    await _check_voice_frame_policy(speech_target(), policy, avatar=False)
    assert store.strict_reads == 1


async def test_avatar_permission_revocation_still_stops_the_next_frame(frame_policy):
    policy, _, _, _, config = frame_policy
    await _check_voice_frame_policy(speech_target(), policy, avatar=True)
    config["domains"]["avatars"]["default"]["allow"] = []
    policy.settings.group_policy_json = json.dumps(config)
    with pytest.raises(PolicyError) as refusal:
        await _check_voice_frame_policy(speech_target(), policy, avatar=True)
    assert refusal.value.decision.reason == "policy_denied"
    # Control: revoking only avatar use does not revoke ordinary voice.
    await _check_voice_frame_policy(speech_target(), policy, avatar=False)


@pytest.mark.parametrize("domain", ["models", "zones"])
async def test_unscoped_speech_never_bypasses_model_or_zone_restrictions(frame_policy, domain):
    policy, _, _, _, config = frame_policy
    config["domains"][domain] = {"default": {"allow": []}}
    policy.settings.group_policy_json = json.dumps(config)
    with pytest.raises(PolicyError) as refusal:
        await _check_voice_frame_policy(speech_target(), policy, avatar=True)
    assert refusal.value.decision.reason == "policy_surface_unsupported"


async def test_owner_store_failure_is_not_a_cached_frame_grant(frame_policy, monkeypatch):
    policy, store, _, _, _ = frame_policy
    await _check_voice_frame_policy(speech_target(), policy, avatar=True)

    async def unavailable(_owner: str):
        raise RuntimeError("Synthetic owner-store outage.")

    monkeypatch.setattr(store, "get_strict", unavailable)
    with pytest.raises(PolicyError) as refusal:
        await _check_voice_frame_policy(speech_target(), policy, avatar=True)
    assert refusal.value.decision.reason == "policy_unavailable"


async def test_configured_budget_is_read_and_enforced_on_every_frame(frame_policy):
    policy, store, reader, _, _ = frame_policy
    await store.put(Entitlement(id="owner", userId="owner", tokensPerDay=100))
    reader.totals = WindowTotals(totalTokens=99)
    await _check_voice_frame_policy(speech_target(), policy, avatar=True)
    assert store.strict_reads == 1 and reader.calls == 1
    reader.totals = WindowTotals(totalTokens=100)
    with pytest.raises(PolicyError):
        await _check_voice_frame_policy(speech_target(), policy, avatar=True)
    assert store.strict_reads == 2 and reader.calls == 2


@pytest.mark.parametrize("change", ["expiry", "configuration", "pause"])
async def test_awaited_budget_cannot_restore_changed_authority(frame_policy, monkeypatch, change):
    policy, store, reader, principal, config = frame_policy
    await store.put(Entitlement(id="owner", userId="owner", tokensPerDay=100))
    await _check_voice_frame_policy(speech_target(), policy, avatar=True)
    original = reader.window_totals

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        if change == "expiry":
            monkeypatch.setattr("ai4ia_api.policy.service.time.time", lambda: principal.policy_claims.expires_at)
        elif change == "configuration":
            config["domains"]["avatars"]["default"]["allow"] = []
            policy.settings.group_policy_json = json.dumps(config)
        else:
            policy.settings.group_policy_enabled = False
        return result

    monkeypatch.setattr(reader, "window_totals", changed)
    with pytest.raises(PolicyError):
        await _check_voice_frame_policy(speech_target(), policy, avatar=True)


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("backend", ["auto", "websockets"])
async def test_native_audio_backpressure_keeps_pongs_live_with_one_fresh_read(
    frame_policy, legacy, backend,
):
    """Drive Uvicorn's real Ping/Pong parser, not TestClient's in-memory socket."""
    policy, store, _, principal, _ = frame_policy
    store.delay = 0.04
    received = []

    async def endpoint(ws):
        bind_authenticated(policy, principal)
        await ws.accept()
        try:
            while True:
                frame = await ws.receive_text()
                if legacy:
                    await authorize_dispatch("realtime", deployment=None, required=True)
                    await require_policy(PolicyRequest("avatar.use"))
                else:
                    await _check_voice_frame_policy(speech_target(), policy, avatar=True)
                received.append(frame)
                await ws.send_text("accepted")
        finally:
            clear_policy_context()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        Starlette(routes=[WebSocketRoute("/voice", endpoint)]),
        host="127.0.0.1", port=port, ws=backend,
        ws_max_queue=2, ws_ping_interval=0.05, ws_ping_timeout=0.3,
        lifespan="off", access_log=False, log_level="critical",
    ))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    audio = json.dumps({"type": "input_audio_buffer.append", "audio": "A" * 6400})
    frames = 20
    sender = None
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        async with connect(f"ws://127.0.0.1:{port}/voice", ping_interval=None) as ws:
            async def send_audio():
                for _ in range(frames):
                    await ws.send(audio)
                    await asyncio.sleep(0.1)

            sender = asyncio.create_task(send_audio())
            started = time.monotonic()
            if legacy:
                with pytest.raises(ConnectionClosedError) as closed:
                    async with asyncio.timeout(5):
                        while True:
                            await ws.recv()
                assert closed.value.rcvd is not None
                assert closed.value.rcvd.code == 1011
                assert closed.value.rcvd.reason == "keepalive ping timeout"
                assert len(received) < frames
            else:
                async with asyncio.timeout(4):
                    for _ in range(frames):
                        assert await ws.recv() == "accepted"
                assert len(received) == frames
                assert store.strict_reads == frames
                assert time.monotonic() - started < 3
    finally:
        if sender is not None:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, 5)
        finally:
            if not serving.done():
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)
            listener.close()
