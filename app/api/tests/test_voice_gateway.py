"""Pure URL/body shaping tests for voice (speech + transcription) gateway requests.

No network: only the request builders are exercised, mirroring the image builder
tests. Guards the two provider styles, the dedicated audio api-version, the
JSON body for speech, and the multipart auth headers for transcription.
"""
from __future__ import annotations

from ai4ia_api.config import GatewayAuthMode, GatewayProviderStyle
from ai4ia_api.gateway.client import ModelGatewayClient
from tests.conftest import make_settings


def _client(**overrides) -> ModelGatewayClient:
    return ModelGatewayClient(make_settings(**overrides))


def test_azure_native_speech_url_and_body():
    c = _client(gateway_audio_api_version="2024-10-21")
    req = c.build_speech_request(
        deployment="tts-hd-dep", text="hello", voice="alloy", response_format="mp3"
    )
    assert req.url == (
        "http://gateway.test/deployments/tts-hd-dep/audio/speech?api-version=2024-10-21"
    )
    assert req.json["input"] == "hello"
    assert req.json["voice"] == "alloy"
    assert req.json["response_format"] == "mp3"
    # The deployment is in the path AND the body: the 2025 speech api-version
    # requires the `model` field (gpt-4o-mini-tts rejects the request without it).
    assert req.json["model"] == "tts-hd-dep"


def test_speech_uses_audio_api_version_not_chat():
    c = _client(gateway_api_version="2099-chat-only", gateway_audio_api_version="2024-10-21")
    req = c.build_speech_request(
        deployment="dep", text="x", voice="nova", response_format="wav"
    )
    assert "api-version=2024-10-21" in req.url
    assert "2099-chat-only" not in req.url


def test_openai_compatible_speech_puts_model_in_body():
    c = _client(gateway_provider_style=GatewayProviderStyle.openai_compatible)
    req = c.build_speech_request(
        deployment="tts-dep", text="hi", voice="echo", response_format="mp3"
    )
    assert req.url == "http://gateway.test/audio/speech"
    assert req.json["model"] == "tts-dep"
    assert "api-version" not in req.url


def test_speech_extra_params_merged():
    c = _client()
    req = c.build_speech_request(
        deployment="dep", text="x", voice="alloy", response_format="mp3",
        extra={"speed": 1.25},
    )
    assert req.json["speed"] == 1.25


def test_azure_native_transcription_url():
    c = _client(gateway_audio_api_version="2024-10-21")
    url = c.transcription_url("whisper-dep")
    assert url == (
        "http://gateway.test/deployments/whisper-dep/audio/transcriptions"
        "?api-version=2024-10-21"
    )


def test_openai_compatible_transcription_url_has_no_api_version():
    c = _client(gateway_provider_style=GatewayProviderStyle.openai_compatible)
    url = c.transcription_url("whisper-dep")
    assert url == "http://gateway.test/audio/transcriptions"
    assert "api-version" not in url


def test_multipart_headers_omit_json_content_type_keep_auth():
    c = _client(model_gateway_auth_mode=GatewayAuthMode.api_key, model_gateway_api_key="k-9")
    headers = c._auth_headers_multipart("corr-1")
    # httpx must set the multipart boundary itself, so no forced JSON content type.
    assert "Content-Type" not in headers
    assert headers["Ocp-Apim-Subscription-Key"] == "k-9"
    assert headers["x-correlation-id"] == "corr-1"
