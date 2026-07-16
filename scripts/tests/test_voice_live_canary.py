from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "voice-live-canary.py"


def load_script():
    spec = importlib.util.spec_from_file_location("voice_live_canary", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load Voice Live canary")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


canary = load_script()

AZURE_UPDATE = (
    '{"type":"session.update","session":{"instructions":"You are a helpful, concise voice '
    'assistant. Keep spoken replies brief and natural.","voice":"alloy",'
    '"input_audio_format":"pcm16","output_audio_format":"pcm16",'
    '"turn_detection":{"type":"server_vad"},'
    '"input_audio_transcription":{"model":"whisper-1"}}}'
)
FIRST_SEED = {
    "type": "conversation.item.create",
    "item": {
        "id": "voice-canary-seed-001",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "voice-canary-user"}],
    },
}
SPEECH_PAIRS = {
    "gpt-realtime": "gpt-4o-transcribe",
    "gpt-realtime-mini": "gpt-4o-transcribe",
    "gpt-4.1": "azure-speech",
    "gpt-4.1-mini": "azure-speech",
    "gpt-5-mini": "azure-speech",
    "gpt-5.1": "azure-speech",
}


class VoiceLiveCanaryTests(unittest.TestCase):
    def test_azure_openai_url_and_first_two_frames_are_canonical(self) -> None:
        url = canary.build_canary_url(
            "wss://api.example.test/api/voice/live",
            provider="azure_openai",
            model="gpt-realtime",
            region="eastus2",
            agent="analyst",
            tools=True,
        )
        self.assertEqual(
            url,
            "wss://api.example.test/api/voice/live"
            "?provider=azure_openai&model=gpt-realtime&region=eastus2"
            "&agent=analyst&tools=1",
        )
        frames = canary.build_initial_frames("azure_openai", "gpt-realtime")
        self.assertEqual(frames[0], AZURE_UPDATE)
        self.assertEqual(json.loads(frames[1]), FIRST_SEED)

    def test_all_six_speech_pairs_have_exact_session_and_seed_shapes(self) -> None:
        for model, transcription in SPEECH_PAIRS.items():
            with self.subTest(model=model):
                url = canary.build_canary_url(
                    "wss://api.example.test/api/voice/live",
                    provider="speech_voice_live",
                    model=model,
                )
                self.assertEqual(
                    url,
                    "wss://api.example.test/api/voice/live"
                    f"?provider=speech_voice_live&model={model}",
                )
                frames = canary.build_initial_frames("speech_voice_live", model)
                self.assertEqual(
                    json.loads(frames[0]),
                    {
                        "type": "session.update",
                        "session": {
                            "instructions": (
                                "You are a helpful, concise voice assistant. "
                                "Keep spoken replies brief and natural."
                            ),
                            "voice": {
                                "type": "azure-standard",
                                "name": "en-US-Ava:DragonHDLatestNeural",
                                "locale": "en-US",
                            },
                            "input_audio_transcription": {
                                "model": transcription,
                                "language": "en-US",
                            },
                            "turn_detection": {
                                "type": "azure_semantic_vad",
                                "interrupt_response": True,
                                "auto_truncate": False,
                            },
                            "input_audio_noise_reduction": {
                                "type": "azure_deep_noise_suppression"
                            },
                            "input_audio_echo_cancellation": {
                                "type": "server_echo_cancellation"
                            },
                        },
                    },
                )
                self.assertEqual(json.loads(frames[1]), FIRST_SEED)

    def test_url_constraints_reject_unsafe_or_provider_invalid_inputs(self) -> None:
        invalid_urls = (
            "ws://api.example.test/api/voice/live",
            "wss://api.example.test/api/voice/live/",
            "wss://api.example.test/not-voice",
            "wss://user:password@api.example.test/api/voice/live",
            "wss://api.example.test/api/voice/live?token=secret",
            "wss://api.example.test/api/voice/live#fragment",
        )
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(canary.CanaryInputError):
                canary.build_canary_url(
                    url,
                    provider="azure_openai",
                    model="gpt-realtime",
                )
        with self.assertRaises(canary.CanaryInputError):
            canary.build_canary_url(
                "wss://api.example.test/api/voice/live",
                provider="speech_voice_live",
                model="gpt-realtime",
                region="eastus2",
            )

    def test_arbitrary_speech_model_is_rejected_before_network(self) -> None:
        with self.assertRaises(canary.CanaryInputError):
            canary.build_initial_frames("speech_voice_live", "arbitrary-model")
        with self.assertRaises(canary.CanaryInputError):
            canary.build_canary_url(
                "wss://api.example.test/api/voice/live",
                provider="speech_voice_live",
                model="arbitrary-model",
            )

    def test_event_order_requires_created_then_updated_and_all_history_acks(self) -> None:
        expected_ids = canary.expected_history_item_ids()
        state = canary.EventState(expected_ids)
        self.assertFalse(
            canary.inspect_event('{"type":"rate_limits.updated"}', state, token="secret")
        )
        self.assertFalse(
            canary.inspect_event(
                '{"type":"session.created","correlationId":"corr-123"}',
                state,
                token="secret",
            )
        )
        self.assertFalse(
            canary.inspect_event('{"type":"session.updated"}', state, token="secret")
        )
        self.assertEqual(state.acknowledged_history_item_ids, set())
        self.assertFalse(
            canary.inspect_event(
                json.dumps(
                    {
                        "type": "conversation.item.created",
                        "item": {"id": expected_ids[0]},
                    }
                ),
                state,
                token="secret",
            )
        )
        self.assertEqual(state.acknowledged_history_item_ids, {expected_ids[0]})
        self.assertTrue(
            canary.inspect_event(
                json.dumps(
                    {
                        "type": "conversation.item.created",
                        "item": {"id": expected_ids[1]},
                    }
                ),
                state,
                token="secret",
            )
        )
        self.assertEqual(state.correlation, "corr-123")

        wrong_order = canary.EventState()
        with self.assertRaises(canary.CanaryOrderError):
            canary.inspect_event(
                '{"type":"session.updated"}', wrong_order, token="secret"
            )

    def test_history_acknowledgements_may_arrive_out_of_order(self) -> None:
        expected_ids = canary.expected_history_item_ids()
        state = canary.EventState(expected_ids)
        self.assertFalse(
            canary.inspect_event('{"type":"session.created"}', state, token="secret")
        )
        self.assertFalse(
            canary.inspect_event('{"type":"session.updated"}', state, token="secret")
        )
        self.assertFalse(
            canary.inspect_event(
                json.dumps(
                    {
                        "type": "conversation.item.created",
                        "item": {"id": expected_ids[1]},
                    }
                ),
                state,
                token="secret",
            )
        )
        self.assertTrue(
            canary.inspect_event(
                json.dumps(
                    {
                        "type": "conversation.item.created",
                        "item": {"id": expected_ids[0]},
                    }
                ),
                state,
                token="secret",
            )
        )

    def test_protocol_output_is_bounded_redacted_and_never_contains_token(self) -> None:
        token = "header.payload.signature"
        expected_ids = canary.expected_history_item_ids()
        state = canary.EventState(expected_ids)
        canary.inspect_event('{"type":"session.created"}', state, token=token)
        canary.inspect_event('{"type":"session.updated"}', state, token=token)
        canary.inspect_event(
            json.dumps(
                {
                    "type": "conversation.item.created",
                    "item": {"id": expected_ids[0]},
                }
            ),
            state,
            token=token,
        )
        message = (
            f"Authorization: Bearer {token} token={token} "
            + "safe"
            + ("x" * 800)
        )
        frame = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "bad_request",
                    "param": "session",
                    "event_id": "event-1",
                    "message": message,
                },
            }
        )
        with self.assertRaises(canary.CanaryProtocolError) as caught:
            canary.inspect_event(frame, state, token=token)

        output = io.StringIO()
        with redirect_stdout(output):
            canary._emit(
                "speech_voice_live",
                "gpt-realtime",
                "protocol_error",
                fields=caught.exception.fields,
            )
        rendered = output.getvalue()
        payload = json.loads(rendered)
        self.assertNotIn(token, rendered)
        self.assertLessEqual(len(payload["message"]), 512)
        self.assertEqual(payload["type"], "invalid_request_error")
        self.assertEqual(payload["code"], "bad_request")
        self.assertEqual(payload["param"], "session")
        self.assertEqual(payload["event_id"], "event-1")

        close = canary.CanaryCloseError(
            1008, f"Bearer {token}", token, correlation=None
        )
        self.assertNotIn(token, close.reason or "")

    def test_close_while_waiting_for_history_ack_is_not_success(self) -> None:
        expected_ids = canary.expected_history_item_ids()

        class FakeWsMessageType:
            TEXT = "text"
            BINARY = "binary"
            CLOSE = "close"
            CLOSING = "closing"
            CLOSED = "closed"
            ERROR = "error"

        messages = [
            SimpleNamespace(
                type=FakeWsMessageType.TEXT,
                data='{"type":"session.created"}',
                extra=None,
            ),
            SimpleNamespace(
                type=FakeWsMessageType.TEXT,
                data='{"type":"session.updated"}',
                extra=None,
            ),
            SimpleNamespace(
                type=FakeWsMessageType.TEXT,
                data=json.dumps(
                    {
                        "type": "conversation.item.created",
                        "item": {"id": expected_ids[0]},
                    }
                ),
                extra=None,
            ),
            SimpleNamespace(
                type=FakeWsMessageType.CLOSE,
                data=None,
                extra="provider stopped",
            ),
        ]

        class FakeWebSocket:
            protocol = canary.BEARER_SUBPROTOCOL
            close_code = 1011
            closed = False

            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send_str(self, frame: str) -> None:
                self.sent.append(frame)

            async def close(self, **_kwargs) -> None:
                self.closed = True

            def __aiter__(self):
                async def iterate():
                    for message in messages:
                        yield message

                return iterate()

        websocket = FakeWebSocket()

        class FakeWebSocketContext:
            async def __aenter__(self):
                return websocket

            async def __aexit__(self, *_args):
                return None

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def ws_connect(self, *_args, **_kwargs):
                return FakeWebSocketContext()

        fake_aiohttp = SimpleNamespace(
            ClientTimeout=lambda **_kwargs: object(),
            ClientSession=lambda **_kwargs: FakeSession(),
            WSMsgType=FakeWsMessageType,
        )
        output = io.StringIO()
        with (
            patch.dict(sys.modules, {"aiohttp": fake_aiohttp}),
            redirect_stdout(output),
        ):
            result = asyncio.run(
                canary.run_canary(
                    url="wss://api.example.test/api/voice/live",
                    origin="https://app.example.test",
                    provider="azure_openai",
                    model="gpt-realtime",
                    token="header.payload.signature",
                    timeout=1,
                )
            )

        self.assertEqual(result, 5)
        self.assertEqual(json.loads(output.getvalue())["outcome"], "closed")
        self.assertEqual(len(websocket.sent), 3)


if __name__ == "__main__":
    unittest.main()
