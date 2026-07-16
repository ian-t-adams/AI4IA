from __future__ import annotations

import pytest

from tests.conftest import make_settings


def _settings(**overrides):
    values = {
        "env": "dev",
        "realtime_enabled": True,
        "realtime_allowed_origins": "https://web.example",
        "model_gateway_auth_mode": "api_key",
        "model_gateway_api_key": "proxy-ingress-key",
        "realtime_base_url": "https://replacement.azure-api.net/openai",
        "realtime_gateway_api_key": "realtime-key",
        "voice_provider_allowlist": "azure_openai",
        "voice_default_provider": "azure_openai",
    }
    values.update(overrides)
    return make_settings(**values)


def test_voice_live_requires_a_distinct_websocket_gateway_contract():
    _settings().validate_runtime()

    with pytest.raises(RuntimeError, match="REALTIME_BASE_URL"):
        _settings(realtime_base_url="").validate_runtime()
    with pytest.raises(RuntimeError, match="REALTIME_GATEWAY_API_KEY"):
        _settings(realtime_gateway_api_key="").validate_runtime()
    with pytest.raises(RuntimeError, match="distinct realtime gateway key"):
        _settings(realtime_gateway_api_key="proxy-ingress-key").validate_runtime()
    with pytest.raises(RuntimeError, match="WebSocket-capable shared active APIM"):
        _settings(realtime_base_url="https://replacement.azure-api.net/not-openai").validate_runtime()


def _speech_settings(**overrides):
    values = {
        "env": "dev",
        "realtime_enabled": True,
        "realtime_allowed_origins": "https://web.example",
        "model_gateway_auth_mode": "api_key",
        "model_gateway_api_key": "proxy-ingress-key",
        "realtime_base_url": "https://replacement.azure-api.net/openai",
        "realtime_gateway_api_key": "realtime-key",
        "speech_voice_live_enabled": True,
        "voice_provider_allowlist": "azure_openai,speech_voice_live",
        "voice_default_provider": "azure_openai",
        "speech_voice_live_base_url": "https://replacement.azure-api.net/speech/voice-live",
        "speech_voice_live_gateway_api_key": "speech-key",
    }
    values.update(overrides)
    return make_settings(**values)


def test_speech_voice_live_runtime_contradictions_are_fail_closed():
    _speech_settings().validate_runtime()

    with pytest.raises(RuntimeError, match="VOICE_DEFAULT_PROVIDER"):
        _speech_settings(
            speech_voice_live_enabled=False,
            voice_default_provider="speech_voice_live",
            voice_provider_allowlist="azure_openai",
        ).validate_runtime()
    with pytest.raises(RuntimeError, match="SPEECH_VOICE_LIVE_ENABLED=true"):
        _speech_settings(
            speech_voice_live_enabled=False,
            voice_provider_allowlist="azure_openai,speech_voice_live",
        ).validate_runtime()
    with pytest.raises(RuntimeError, match="requires AI4IA_REALTIME_ENABLED=true"):
        _speech_settings(realtime_enabled=False).validate_runtime()
    with pytest.raises(RuntimeError, match="SPEECH_VOICE_LIVE_BASE_URL"):
        _speech_settings(speech_voice_live_base_url="").validate_runtime()
    with pytest.raises(RuntimeError, match="distinct gateway key"):
        _speech_settings(speech_voice_live_gateway_api_key="realtime-key").validate_runtime()
    with pytest.raises(RuntimeError, match="distinct gateway key"):
        _speech_settings(speech_voice_live_gateway_api_key="proxy-ingress-key").validate_runtime()
    with pytest.raises(RuntimeError, match="APIM-style HTTPS/WSS base URL"):
        _speech_settings(
            speech_voice_live_base_url="https://services.ai.azure.com/speech/voice-live",
        ).validate_runtime()
