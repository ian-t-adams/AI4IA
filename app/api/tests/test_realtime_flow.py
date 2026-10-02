"""Content-free conversation-flow telemetry for live voice sessions.

Counts are bounded by fixed allowlists, response outcomes by the documented
status/reason enums, and only ``response.done`` payloads are parsed for status
(below a size bound). No transcript, id, token or free text reaches the
outcome, the completion log line or the custom event.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from ai4ia_api import realtime_flow
from ai4ia_api.realtime_flow import (
    CLIENT_FLOW_EVENTS,
    RESPONSE_DONE_MAX_CHARS,
    RESPONSE_OUTCOMES,
    UPSTREAM_FLOW_EVENTS,
    event_properties,
    response_outcome,
)
from ai4ia_api.routers.realtime import DEV_SUBPROTOCOL, UpstreamMessage, relay
from tests.test_realtime_api import (
    ScriptedRealtimeConnector,
    _attach_completion_capture,
    _client,
    _completion_payloads,
    _origin,
)
from tests.test_realtime_logic import (
    _live_avatar,
    _relay_bridge,
    _RelayClient,
    _RelayUpstream,
    _video_frame,
)

SECRET = "PRIVATE-TRANSCRIPT-SENTINEL"


def _done(status: object, reason: object = None, **extra) -> str:
    response: dict[str, object] = {
        "id": "resp_secret_id", "status": status,
        "output": [{"type": "message", "content": [{"type": "audio", "transcript": SECRET}]}],
        "usage": {"total_tokens": 42},
        **extra,
    }
    if reason is not None:
        response["status_details"] = {"type": status, "reason": reason}
    return json.dumps({"type": "response.done", "event_id": "evt_secret", "response": response})


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("frame", "label"), [
    (_done("completed"), "completed"),
    (_done("cancelled", "turn_detected"), "cancelled:turn_detected"),
    (_done("cancelled", "client_cancelled"), "cancelled:client_cancelled"),
    (_done("cancelled", "a new provider reason"), "cancelled:other"),
    (_done("cancelled"), "cancelled:other"),
    (_done("cancelled", ["unhashable"]), "cancelled:other"),
    (_done("incomplete", "max_output_tokens"), "incomplete:max_output_tokens"),
    (_done("incomplete", "content_filter"), "incomplete:content_filter"),
    (_done("incomplete", "turn_detected"), "incomplete:other"),
    (_done("failed", None, status_details={"error": {"message": SECRET}}), "failed"),
    (_done("in_progress"), "other"),
    (_done("exploded"), "other"),
    (_done(["completed"]), "other"),
    ('{"type":"response.done"}', "other"),
    ('{"type":"response.done","response":"completed"}', "other"),
    ("not json", "other"),
])
def test_response_outcome_admits_only_documented_status_and_reason_values(frame, label):
    assert response_outcome(frame) == label
    assert label in RESPONSE_OUTCOMES


def test_response_outcome_skips_oversized_payloads():
    def padded(total: int) -> str:
        base = _done("completed")
        return base[:-2] + ',"pad":"' + "x" * (total - len(base) - 9) + '"}}'

    below, above = padded(RESPONSE_DONE_MAX_CHARS), padded(RESPONSE_DONE_MAX_CHARS + 1)
    assert len(below) == RESPONSE_DONE_MAX_CHARS and len(above) == RESPONSE_DONE_MAX_CHARS + 1
    assert response_outcome(below) == "completed"  # control: the same payload under the bound
    assert response_outcome(above) == "other"


def test_property_names_are_unique_and_events_drop_unknown_keys_and_zeros():
    names = [*CLIENT_FLOW_EVENTS.values(), *UPSTREAM_FLOW_EVENTS.values(), *RESPONSE_OUTCOMES.values()]
    assert len(names) == len(set(names))
    assert "flowVersion" not in names
    properties = event_properties(
        [("response.cancel", 2), ("not.allowlisted", 9)],
        [("input_audio_buffer.speech_started", 3), ("response.done", 0)],
        [("cancelled:turn_detected", 1), ("invented", 4)],
    )
    assert properties == {
        "flowVersion": 1,
        "clientResponseCancel": 2,
        "upSpeechStarted": 3,
        "responseCancelledTurnDetected": 1,
    }


# --------------------------------------------------------------------------- #
# The relay's own counting.
# --------------------------------------------------------------------------- #


def _run(*, client=(), upstream=(), avatar=None):
    return asyncio.run(relay(
        _RelayClient(client), _RelayUpstream(upstream), max_seconds=1,
        bridge=_relay_bridge(), avatar=avatar,
    ))


def test_client_flow_counts_only_allowlisted_events_and_keep_no_content():
    append = json.dumps({"type": "input_audio_buffer.append", "audio": SECRET})
    frames = [
        *[{"type": "websocket.receive", "text": append}] * 3,
        {"type": "websocket.receive", "text": '{"type":"response.cancel"}'},
        {"type": "websocket.receive", "text": '{"type":"output_audio_buffer.clear"}'},
        {"type": "websocket.receive", "text": '{"type":"conversation.item.truncate","item_id":"x"}'},
        {"type": "websocket.receive", "text": '{"type":"browser.invented"}'},
        {"type": "websocket.receive", "text": "not json"},
        {"type": "websocket.disconnect", "code": 1000},
    ]
    outcome = _run(client=frames)
    stats = outcome.stats.client_to_upstream
    assert stats.text_frames == 8
    assert dict(stats.event_counts) == {
        "input_audio_buffer.append": 3,
        "conversation.item.truncate": 1,
        "response.cancel": 1,
        "output_audio_buffer.clear": 1,
    }
    # Allowlist order, so identical sessions serialize identically.
    assert [key for key, _ in stats.event_counts] == [
        key for key in CLIENT_FLOW_EVENTS if key in dict(stats.event_counts)
    ]
    assert SECRET not in repr(outcome)


def test_upstream_flow_and_response_outcomes_are_counted_without_content():
    frames = [
        '{"type":"input_audio_buffer.speech_started","item_id":"item_secret"}',
        '{"type":"input_audio_buffer.speech_stopped"}',
        json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                    "transcript": SECRET}),
        '{"type":"response.created"}',
        _done("cancelled", "turn_detected"),
        json.dumps({"type": "response.audio.delta", "delta": SECRET}),
        '{"type":"response.created"}',
        _done("completed"),
        _done("cancelled", "turn_detected"),
        '{"type":"session.avatar.switch_to_speaking"}',
        '{"type":"provider.invented"}',
    ]
    outcome = _run(upstream=[
        *[UpstreamMessage("text", text=frame) for frame in frames],
        UpstreamMessage("close", close_code=1000),
    ])
    assert outcome.status == "complete"
    assert dict(outcome.stats.upstream_to_client.event_counts) == {
        "input_audio_buffer.speech_started": 1,
        "input_audio_buffer.speech_stopped": 1,
        "conversation.item.input_audio_transcription.completed": 1,
        "response.created": 2,
        "response.done": 3,
        "session.avatar.switch_to_speaking": 1,
    }
    assert dict(outcome.stats.response_outcomes) == {
        "completed": 1, "cancelled:turn_detected": 2,
    }
    for forbidden in (SECRET, "item_secret", "resp_secret_id", "evt_secret"):
        assert forbidden not in repr(outcome)


def test_counts_stay_bounded_however_many_event_types_arrive():
    frames = [
        {"type": "websocket.receive", "text": json.dumps({"type": f"browser.event_{index}"})}
        for index in range(300)
    ] + [{"type": "websocket.receive", "text": '{"type":"input_audio_buffer.append"}'}] * 300
    outcome = _run(client=[*frames, {"type": "websocket.disconnect", "code": 1000}])
    assert dict(outcome.stats.client_to_upstream.event_counts) == {"input_audio_buffer.append": 300}


@pytest.mark.parametrize("with_avatar", [False, True])
def test_only_response_done_payloads_are_parsed_for_their_outcome(monkeypatch, with_avatar):
    seen: list[str] = []
    real = realtime_flow.response_outcome

    def spy(frame: str) -> str:
        seen.append(frame)
        return real(frame)

    monkeypatch.setattr(realtime_flow, "response_outcome", spy)
    done = _done("cancelled", "client_cancelled")
    frames = [
        json.dumps({"type": "response.audio.delta", "delta": "AAAA"}),
        *([_video_frame(1024)] * 3 if with_avatar else []),
        '{"type":"response.audio_transcript.done","transcript":"x"}',
        done,
    ]
    avatar = _live_avatar() if with_avatar else None
    outcome = _run(
        upstream=[*[UpstreamMessage("text", text=f) for f in frames], UpstreamMessage("close", close_code=1000)],
        avatar=avatar,
    )
    assert seen == [done]
    assert dict(outcome.stats.response_outcomes) == {"cancelled:client_cancelled": 1}
    if with_avatar:
        assert avatar is not None and avatar.video_frames == 3


# --------------------------------------------------------------------------- #
# The real route: completion log line and custom event.
# --------------------------------------------------------------------------- #


def test_completion_log_and_custom_event_carry_bounded_numeric_flow(caplog, monkeypatch):
    from ai4ia_api.routers import realtime as realtime_module

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        realtime_module, "emit_custom_event", lambda name, attrs: events.append((name, attrs)),
    )
    c = _client(realtime_enabled=True)
    caplog.set_level("INFO", logger="ai4ia_api.routers.realtime")
    capture = _attach_completion_capture(caplog)
    try:
        frames = [
            '{"type":"input_audio_buffer.speech_started"}',
            _done("cancelled", "turn_detected"),
            '{"type":"input_audio_buffer.speech_started"}',
            _done("completed"),
        ]
        c.app.state.realtime_connector = ScriptedRealtimeConnector([
            *[UpstreamMessage("text", text=frame) for frame in frames],
            UpstreamMessage("close", close_code=1000),
        ])
        with c.websocket_connect(
            "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "flowuser"], headers=_origin(),
        ) as ws:
            for _ in frames:
                ws.receive_text()
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()

        payloads = _completion_payloads(caplog)
        assert len(payloads) == 1
        stats = payloads[0]["stats"]
        assert stats["upstreamToClient"]["eventCounts"] == {
            "input_audio_buffer.speech_started": 2, "response.done": 2,
        }
        assert stats["responseOutcomes"] == {"cancelled:turn_detected": 1, "completed": 1}

        live = [attrs for name, attrs in events if name == "voice_live_completion"]
        assert len(live) == 1
        flow = {key: value for key, value in live[0].items() if key in {
            "flowVersion", *UPSTREAM_FLOW_EVENTS.values(), *CLIENT_FLOW_EVENTS.values(),
            *RESPONSE_OUTCOMES.values(),
        }}
        assert flow == {
            "flowVersion": 1,
            "upSpeechStarted": 2,
            "upResponseDone": 2,
            "responseCancelledTurnDetected": 1,
            "responseCompleted": 1,
        }
        assert all(type(value) is int for value in flow.values())
        # The existing fields the admin VOICE_KQL reads are unchanged.
        for key in ("provider", "model", "outcome", "closeCode", "durationMs"):
            assert key in live[0]
        encoded = json.dumps(payloads) + json.dumps(live, default=str)
        for forbidden in (SECRET, "resp_secret_id", "evt_secret", "flowuser"):
            assert forbidden not in encoded
    finally:
        if capture is not None:
            capture.removeHandler(caplog.handler)
        c.__exit__(None, None, None)
