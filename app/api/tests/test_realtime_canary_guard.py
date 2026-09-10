"""One-open GA envelope with current authority, no audio/response/tool escape."""

import json
from unittest.mock import AsyncMock

import pytest

from ai4ia_api.realtime_canary import (
    SETUP_INPUT, RealtimeSetup, RealtimeSetupRejected,
    current_realtime_setup, realtime_canary_dispatch_guard, realtime_setup_scope,
)

UPDATE = json.dumps({
    "type": "session.update",
    "session": {
        "type": "realtime", "model": "catalog-deployment",
        "output_modalities": ["text"], "audio": {"input": {"turn_detection": None}},
        "max_output_tokens": 64, "tools": [], "tool_choice": "none",
    },
})
OPEN = {
    "operation": "session_open", "endpoint": "https://gateway.test/openai/v1/realtime?model=catalog-deployment",
    "protocol": "ga", "provider": "azure_openai",
}


def setup(authorize=None, clock=None):
    return RealtimeSetup(
        "owner", "catalog-deployment", OPEN["endpoint"], UPDATE,
        authorize or AsyncMock(return_value=True), **({"clock": clock} if clock else {}),
    )


async def test_setup_uses_one_open_one_update_and_causal_ordered_events():
    guard = setup()
    assert not await realtime_canary_dispatch_guard("owner", "catalog-deployment", OPEN)
    with realtime_setup_scope(guard):
        assert await realtime_canary_dispatch_guard("owner", "catalog-deployment", OPEN)
        assert not await realtime_canary_dispatch_guard("owner", "catalog-deployment", OPEN)
        assert not await guard.server_frame(text='{"type":"session.created"}', data=None)
        guard.client_frame(SETUP_INPUT)
        await guard.before_send(text=UPDATE, data=None)
        assert await guard.server_frame(text='{"type":"session.updated"}', data=None)
        with pytest.raises(RealtimeSetupRejected):
            await guard.before_send(text=UPDATE, data=None)
    assert current_realtime_setup() is None


@pytest.mark.parametrize("change", [
    {"protocol": "preview"}, {"provider": "speech_voice_live"}, {"operation": "response_create"},
    {"endpoint": "https://unapproved.test"}, {"tools": True},
])
async def test_open_payload_cannot_widen_server_bound_target(change):
    guard = setup()
    with realtime_setup_scope(guard):
        assert not await realtime_canary_dispatch_guard("owner", "catalog-deployment", {**OPEN, **change})
        assert not await realtime_canary_dispatch_guard("other", "catalog-deployment", OPEN)
        assert not await realtime_canary_dispatch_guard("owner", "other", OPEN)
        assert await realtime_canary_dispatch_guard("owner", "catalog-deployment", OPEN)


@pytest.mark.parametrize("frame", [
    '{"type":"response.create"}', '{"type":"input_audio_buffer.append","audio":"AAAA"}',
    '{"type":"conversation.item.create","item":{}}',
    '{"type":"session.update","type":"response.create"}',
    '{"type":"session.update","session":{"instructions":"Replace authority"}}',
    SETUP_INPUT.replace("64", "64.0"),
])
async def test_client_frames_must_be_exactly_the_setup_template(frame):
    guard = setup()
    with realtime_setup_scope(guard):
        assert await realtime_canary_dispatch_guard("owner", "catalog-deployment", OPEN)
        with pytest.raises(RealtimeSetupRejected):
            guard.client_frame(frame)
        guard.client_frame(SETUP_INPUT)
        await guard.before_send(text=UPDATE, data=None)


async def test_binary_and_unrequested_server_events_are_never_forwardable():
    guard = setup()
    with realtime_setup_scope(guard):
        assert await realtime_canary_dispatch_guard("owner", "catalog-deployment", OPEN)
        guard.client_frame(SETUP_INPUT)
        with pytest.raises(RealtimeSetupRejected):
            await guard.before_send(text=None, data=b"audio")
        with pytest.raises(RealtimeSetupRejected):
            await guard.server_frame(text=None, data=b"audio")
        with pytest.raises(RealtimeSetupRejected):
            await guard.server_frame(text='{"type":"response.created"}', data=None)
        with pytest.raises(RealtimeSetupRejected):
            await guard.server_frame(text='{"type":"session.updated"}', data=None)
        await guard.before_send(text=UPDATE, data=None)
        assert not await guard.server_frame(text='{"type":"session.created"}', data=None)
        assert await guard.server_frame(text='{"type":"session.updated"}', data=None)


async def test_revocation_and_deadline_are_rechecked_at_frame_delivery():
    for expire in (False, True):
        clock = [0.0]
        authority = AsyncMock(return_value=True)
        guard = setup(authority, lambda: clock[0])
        with realtime_setup_scope(guard):
            assert await realtime_canary_dispatch_guard("owner", "catalog-deployment", OPEN)
            guard.client_frame(SETUP_INPUT)
            if expire:
                clock[0] = 16
            else:
                authority.return_value = False
            with pytest.raises(RealtimeSetupRejected):
                await guard.before_send(text=UPDATE, data=None)
        authority.return_value = True
        fresh = setup(authority, lambda: clock[0])
        with realtime_setup_scope(fresh):
            assert await realtime_canary_dispatch_guard("owner", "catalog-deployment", OPEN)
            fresh.client_frame(SETUP_INPUT)
            await fresh.before_send(text=UPDATE, data=None)
