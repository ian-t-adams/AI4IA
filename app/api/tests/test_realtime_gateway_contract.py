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
