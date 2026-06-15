"""Config fail-closed checks for custom tools / BYO MCP (Phase 12A + 12B).

The feature needs durable cross-session storage (the per-user MCP registry) and,
once secrets are durable (12B), a Key Vault to hold connection credentials.
``validate_runtime()`` must reject a deployed env that is missing either, while
leaving local/dev and the default-OFF posture untouched.
"""
from __future__ import annotations

import pytest

from ai4ia_api.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        env="local",
        auth_provider="dev",
        allow_dev_auth=True,
        session_store="memory",
        model_gateway_url="http://gateway.test",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_disabled_by_default_validates():
    s = _settings()
    assert s.custom_tools_enabled is False
    s.validate_runtime()  # no raise


def test_enabled_local_is_allowed_without_cosmos_or_vault():
    # Local dev may use the in-memory registry + secret store freely.
    _settings(custom_tools_enabled=True).validate_runtime()


def test_enabled_deployed_with_memory_store_is_rejected():
    s = _settings(env="dev", custom_tools_enabled=True, session_store="memory")
    with pytest.raises(RuntimeError, match="cosmos session store"):
        s.validate_runtime()


def test_enabled_deployed_without_vault_is_rejected():
    s = _settings(
        env="dev",
        custom_tools_enabled=True,
        session_store="cosmos",
        cosmos_endpoint="https://cosmos.example/",
    )
    with pytest.raises(RuntimeError, match="Key Vault"):
        s.validate_runtime()


def test_enabled_deployed_with_cosmos_and_vault_is_allowed():
    s = _settings(
        env="dev",
        custom_tools_enabled=True,
        session_store="cosmos",
        cosmos_endpoint="https://cosmos.example/",
        custom_tools_secret_vault_uri="https://vault.example.net/",
    )
    s.validate_runtime()  # no raise
