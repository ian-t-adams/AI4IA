"""Offline protocol examples independently shared with the browser tests."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest

from ai4ia_api.realtime_protocol import (
    RealtimeProtocol,
    RealtimeProtocolError,
    rewrite_ga_upstream_frame,
    rewrite_openai_client_frame,
)
from ai4ia_api.routers.realtime import inject_session_tools, reject_client_system_message
from tests.test_realtime_logic import _enabled_bridge

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "realtime_protocol.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("protocol", list(RealtimeProtocol))
@pytest.mark.parametrize("case", FIXTURES["client"], ids=lambda case: case["name"])
def test_client_protocol_fixtures(protocol, case):
    frame = json.dumps(case["application"], indent=2)
    rewritten = rewrite_openai_client_frame(
        frame, protocol=protocol, deployment=FIXTURES["deployment"]
    )
    if protocol == RealtimeProtocol.preview:
        assert rewritten == frame
    else:
        assert json.loads(rewritten) == case["ga"]


@pytest.mark.parametrize("case", FIXTURES["server"], ids=lambda case: case["name"])
def test_ga_server_protocol_fixtures(case):
    assert json.loads(rewrite_ga_upstream_frame(json.dumps(case["ga"]))) == case["application"]


@pytest.mark.parametrize("protocol", list(RealtimeProtocol))
@pytest.mark.parametrize(
    "payload",
    [
        {"type": "input_audio_buffer.append", "audio": "AAA="},
        {"type": "input_audio_buffer.commit", "event_id": "commit-1"},
        {"type": "input_audio_buffer.clear"},
        {"type": "response.cancel", "response_id": "resp_1"},
        {"type": "conversation.item.truncate", "item_id": "item_1", "content_index": 0, "audio_end_ms": 40},
        {"type": "conversation.item.delete", "item_id": "item_1"},
        {"type": "conversation.item.retrieve", "item_id": "item_1"},
        {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "call_1", "output": '{"type":"text","model":"opaque"}'}},
        {"type": "response.create"},
        {"type": "future.event", "audio": {"type": "text"}, "model": "opaque"},
    ],
)
def test_unaffected_client_frames_keep_bytes(protocol, payload):
    frame = json.dumps(payload, indent=2)
    assert rewrite_openai_client_frame(
        frame, protocol=protocol, deployment=FIXTURES["deployment"]
    ) == frame


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "input_audio_buffer.speech_started", "audio_start_ms": 50, "item_id": "user_1"},
        {"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 250, "item_id": "user_1"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "user_1", "transcript": "Hi"},
        {"type": "conversation.item.truncated", "item_id": "item_1", "audio_end_ms": 40, "content_index": 0},
        {"type": "response.function_call_arguments.done", "call_id": "call_1", "name": "calculator", "arguments": '{"type":"output_text","expression":"2+3"}'},
        {"type": "response.done", "response": {"id": "resp_1", "status": "cancelled", "status_details": {"type": "cancelled", "reason": "client_cancelled"}, "usage": None}},
        {"type": "error", "error": {"code": "invalid_request_error", "message": "Provider rejected a parameter", "event_id": "event_1"}},
        {"type": "future.event", "item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Opaque"}]}},
    ],
)
def test_unaffected_server_frames_keep_bytes(payload):
    frame = json.dumps(payload, indent=2)
    assert rewrite_ga_upstream_frame(frame) == frame


@pytest.mark.parametrize("protocol", list(RealtimeProtocol))
def test_catalog_target_is_owned_even_for_encoded_event_types(protocol):
    frame = '{"type":"session\\u002eupdate","session":{"type":"transcription","model":"hostile","deployment":"hostile","voice":"alloy"}}'
    session = json.loads(rewrite_openai_client_frame(
        frame, protocol=protocol, deployment=FIXTURES["deployment"]
    ))["session"]
    assert "deployment" not in session
    if protocol == RealtimeProtocol.ga:
        assert session["type"] == "realtime"
        assert session["model"] == FIXTURES["deployment"]
    else:
        assert "model" not in session
        assert "type" not in session
    response = json.loads(rewrite_openai_client_frame(
        '{"type":"response.create","response":{"model":"hostile","deployment":"hostile","metadata":{"id":"preserve"}}}',
        protocol=protocol, deployment=FIXTURES["deployment"],
    ))
    assert response["response"] == {"metadata": {"id": "preserve"}}


@pytest.mark.parametrize("protocol", list(RealtimeProtocol))
@pytest.mark.parametrize("with_tools", [False, True])
def test_session_and_response_overrides_cannot_change_the_tool_or_persona_contract(protocol, with_tools):
    tools = [{"type": "function", "name": "calculator", "parameters": {"type": "object"}}] if with_tools else []
    for event, key in (("session.update", "session"), ("response.create", "response")):
        frame = json.dumps({
            "type": event,
            key: {
                "instructions": "Browser persona",
                "prompt": {"id": "hosted-unreviewed-prompt"},
                "tools": [{"type": "mcp", "server_url": "https://unapproved.example"}],
                "tool_choice": "required",
                "voice": "alloy",
            },
        }).replace(event, event.replace(".", "\\u002e"))
        adapted = rewrite_openai_client_frame(
            frame, protocol=protocol, deployment=FIXTURES["deployment"]
        )
        governed = json.loads(inject_session_tools(adapted, tools, "auto", instructions="Server persona"))[key]
        assert governed["instructions"] == "Server persona"
        assert "prompt" not in governed
        if key == "session":
            assert governed["tools"] == tools
            assert governed["tool_choice"] == ("auto" if with_tools else "none")
        else:
            assert "tools" not in governed
            assert "tool_choice" not in governed


@pytest.mark.parametrize("protocol", list(RealtimeProtocol))
@pytest.mark.parametrize("event", ["conversation.item.create", "response.create"])
def test_system_item_rejection_has_identical_user_item_control(protocol, event):
    for role in ("system", "developer", "user", "assistant"):
        item = {"type": "message", "role": role, "content": [{"type": "input_text", "text": "Content"}]}
        payload = {"type": event}
        if event == "conversation.item.create":
            payload["item"] = item
        else:
            payload["response"] = {"input": [item]}
        frame = rewrite_openai_client_frame(
            json.dumps(payload), protocol=protocol, deployment=FIXTURES["deployment"]
        )
        assert reject_client_system_message(frame) == (None if role in {"system", "developer"} else frame)


@pytest.mark.parametrize(
    "invalid",
    [
        "not-json", "[]", '{"session":{}}',
        '{"type":"session.update","session":[]}',
        '{"type":"session.update","session":{"audio":{"output":{"voice":"alloy"}}}}',
        '{"type":"session.update","session":{"output_modalities":["audio"]}}',
        '{"type":"session.update","session":{"max_output_tokens":10}}',
        '{"type":"session.update","session":{"input_audio_format":"unknown"}}',
        '{"type":"session.update","session":{"modalities":[]}}',
        '{"type":"response.create","response":{"modalities":["image"]}}',
    ],
)
def test_ga_invalid_application_frames_fail_explicitly_with_a_valid_control(invalid):
    with pytest.raises(RealtimeProtocolError):
        rewrite_openai_client_frame(invalid, protocol=RealtimeProtocol.ga, deployment=FIXTURES["deployment"])
    valid = FIXTURES["client"][0]
    assert json.loads(rewrite_openai_client_frame(
        json.dumps(valid["application"]), protocol=RealtimeProtocol.ga, deployment=FIXTURES["deployment"]
    )) == valid["ga"]


def test_unrecognized_upstream_audio_format_is_not_fabricated_pcm():
    frame = {"type": "session.updated", "session": {"audio": {"output": {"format": {"type": "audio/pcm", "rate": 16000}, "future_setting": True}}}}
    session = json.loads(rewrite_ga_upstream_frame(json.dumps(frame)))["session"]
    assert session["output_audio_format"] == {"type": "audio/pcm", "rate": 16000}
    assert session["audio"] == {"output": {"future_setting": True}}


@pytest.mark.parametrize(
    "event",
    ["conversation.item.done", "conversation.item.retrieved", "response.output_item.added"],
)
def test_other_item_envelopes_translate_without_duplicating_created_events(event):
    item = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello"}]}
    result = json.loads(rewrite_ga_upstream_frame(json.dumps({"type": event, "item": item})))
    assert result["type"] == event
    assert result["item"]["content"] == [{"type": "text", "text": "Hello"}]


@pytest.mark.parametrize("protocol", list(RealtimeProtocol))
def test_registered_but_unoffered_tool_has_an_offered_execution_control(protocol, monkeypatch):
    bridge = _enabled_bridge()
    bridge.tools = [tool for tool in bridge.tools if tool["name"] == "calculator"]
    execute = AsyncMock(wraps=bridge.executor.execute)
    monkeypatch.setattr(bridge.executor, "execute", execute)
    for name in ("get_current_time", "calculator"):
        frame = json.dumps({
            "type": "response.function_call_arguments.done", "call_id": "call_1",
            "name": name, "arguments": '{"expression":"2+3"}',
        })
        if protocol == RealtimeProtocol.ga:
            frame = rewrite_ga_upstream_frame(frame)
        result = anyio.run(bridge.handle_upstream_frame, frame)
        output = json.loads(json.loads(result[0])["item"]["output"])
        if name == "get_current_time":
            assert "error" in output
            assert execute.await_count == 0
        else:
            assert output["result"] == 5
            assert execute.await_count == 1
