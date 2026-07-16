from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

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

    def test_event_order_requires_created_then_updated_and_ignores_safe_events(self) -> None:
        state = canary.EventState()
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
        self.assertTrue(
            canary.inspect_event('{"type":"session.updated"}', state, token="secret")
        )
        self.assertEqual(state.correlation, "corr-123")

        wrong_order = canary.EventState()
        with self.assertRaises(canary.CanaryOrderError):
            canary.inspect_event(
                '{"type":"session.updated"}', wrong_order, token="secret"
            )

    def test_protocol_output_is_bounded_redacted_and_never_contains_token(self) -> None:
        token = "header.payload.signature"
        state = canary.EventState()
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


if __name__ == "__main__":
    unittest.main()
