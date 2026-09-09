from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings


def _settings(**overrides):
    values = {
        "env": "dev",
        "realtime_enabled": True,
        "realtime_allowed_origins": "https://web.example",
        "model_gateway_url": "https://proxy.example.test/openai",
        "model_gateway_auth_mode": "api_key",
        "model_gateway_api_key": "proxy-ingress-key",
        "model_gateway_api_key_header": "S7P-KEY",
        "model_gateway_allowed_hosts": "proxy.example.test",
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


def _ga_settings(**overrides):
    return _settings(**{
        "realtime_ga_enabled": True,
        "realtime_ga_base_url": "https://replacement.azure-api.net/openai/v1",
        "realtime_ga_gateway_api_key": "ga-realtime-key",
        **overrides,
    })


def test_ga_staging_and_protocol_selection_are_independent_default_off_controls():
    settings = _settings()
    assert not settings.realtime_ga_enabled
    assert settings.realtime_protocol == "preview"
    settings.validate_runtime()
    staged = _ga_settings()
    assert staged.realtime_protocol == "preview"
    staged.validate_runtime()
    _ga_settings(realtime_protocol="ga").validate_runtime()
    with pytest.raises(RuntimeError, match="REALTIME_GA_ENABLED=true"):
        _settings(realtime_protocol="ga").validate_runtime()
    with pytest.raises(ValidationError):
        _settings(realtime_protocol="automatic")


@pytest.mark.parametrize("protocol", ["preview", "ga"])
@pytest.mark.parametrize("overrides, message", [
    ({"realtime_enabled": False}, "REALTIME_ENABLED=true"),
    ({"realtime_ga_base_url": ""}, "REALTIME_GA_BASE_URL"),
    ({"realtime_ga_gateway_api_key": ""}, "REALTIME_GA_GATEWAY_API_KEY"),
])
def test_ga_prerequisites_have_a_valid_same_protocol_control(protocol, overrides, message):
    with pytest.raises(RuntimeError, match=message):
        _ga_settings(realtime_protocol=protocol, **overrides).validate_runtime()
    _ga_settings(realtime_protocol=protocol).validate_runtime()


@pytest.mark.parametrize("key_name, key", [
    ("model_gateway_api_key", "proxy-ingress-key"),
    ("realtime_gateway_api_key", "realtime-key"),
    ("speech_voice_live_gateway_api_key", "speech-key"),
    ("official_mcp_subscription_key", "mcp-key"),
    ("code_interpreter_api_key", "sandbox-key"),
])
def test_ga_key_cannot_reuse_any_other_gateway_plane(key_name, key):
    with pytest.raises(RuntimeError, match="distinct API-scoped gateway key"):
        _ga_settings(realtime_ga_gateway_api_key=key, **{key_name: key}).validate_runtime()
    _ga_settings(**{key_name: key}).validate_runtime()


@pytest.mark.parametrize("url", [
    "http://replacement.azure-api.net/openai/v1",
    "https://replacement.azure-api.net/openai",
    "https://different.azure-api.net/openai/v1",
    "https://replacement.azure-api.net/openai/v1?api-version=preview",
    "https://replacement.azure-api.net/openai/v1#fragment",
    "https://replacement.azure-api.net/openai/v1;params",
    "https://user:password@replacement.azure-api.net/openai/v1",
])
def test_ga_url_requires_the_clean_separately_scoped_apim_path(url):
    with pytest.raises(RuntimeError, match="same shared active APIM"):
        _ga_settings(realtime_ga_base_url=url).validate_runtime()
    _ga_settings().validate_runtime()


@pytest.mark.parametrize("suffix", [
    "openai.azure.com", "services.ai.azure.com", "cognitiveservices.azure.com",
])
@pytest.mark.parametrize("host_prefix", ["", "resource."])
def test_ga_rejects_direct_foundry_even_when_the_legacy_url_matches(suffix, host_prefix):
    host = f"{host_prefix}{suffix}"
    with pytest.raises(RuntimeError, match="direct Foundry"):
        _ga_settings(
            realtime_base_url=f"https://{host}/openai",
            realtime_ga_base_url=f"https://{host}/openai/v1",
        ).validate_runtime()
    _ga_settings().validate_runtime()


def test_ga_accepts_wss_custom_gateway_with_distinct_keys():
    _ga_settings(
        realtime_base_url="https://gateway.example.test/openai",
        realtime_ga_base_url="wss://gateway.example.test/openai/v1/",
    ).validate_runtime()


def _speech_settings(**overrides):
    values = {
        "env": "dev",
        "realtime_enabled": True,
        "realtime_allowed_origins": "https://web.example",
        "model_gateway_url": "https://proxy.example.test/openai",
        "model_gateway_auth_mode": "api_key",
        "model_gateway_api_key": "proxy-ingress-key",
        "model_gateway_api_key_header": "S7P-KEY",
        "model_gateway_allowed_hosts": "proxy.example.test",
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


@pytest.mark.parametrize(
    "foundry_host",
    (
        "services.ai.azure.com",
        "resource.services.ai.azure.com",
        "cognitiveservices.azure.com",
        "resource.cognitiveservices.azure.com",
    ),
)
def test_speech_voice_live_rejects_direct_foundry_host_boundaries(
    foundry_host: str,
):
    with pytest.raises(RuntimeError, match="APIM-style HTTPS/WSS base URL"):
        _speech_settings(
            speech_voice_live_base_url=f"https://{foundry_host}/speech/voice-live",
        ).validate_runtime()


@pytest.mark.parametrize(
    "gateway_host",
    (
        "services.ai.azure.com.attacker.test",
        "attacker-services.ai.azure.com",
        "gateway.example-services.ai.azure.com.attacker.test",
    ),
)
def test_speech_voice_live_does_not_misclassify_apim_lookalike_hostname(
    gateway_host: str,
):
    _speech_settings(
        speech_voice_live_base_url=f"https://{gateway_host}/speech/voice-live",
    ).validate_runtime()
