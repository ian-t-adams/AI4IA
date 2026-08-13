"""Config fail-closed checks for custom tools / BYO MCP.

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
        model_gateway_url="https://proxy.test/openai",
        model_gateway_auth_mode="api_key",
        model_gateway_api_key="proxy-secret",
        model_gateway_api_key_header="S7P-KEY",
        model_gateway_allowed_hosts="proxy.test",
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
    # A deployed env with custom tools on must use real (entra) auth — see the
    # auth guard below — so this storage-invariant case uses entra to stay valid.
    s = _settings(
        env="dev",
        auth_provider="entra",
        entra_tenant_id="t1",
        entra_audience="api://ai4ia",
        custom_tools_enabled=True,
        session_store="cosmos",
        cosmos_endpoint="https://cosmos.example/",
        custom_tools_secret_vault_uri="https://vault.example.net/",
    )
    s.validate_runtime()  # no raise


# --- Auth guard: custom tools require real auth in a deployed env -------------
# Per-user custom tools scope the registry + secrets to the signed-in tenant
# user, so spoofable dev auth must not be used once deployed. This guard is
# inert today (custom tools are default-OFF). Cases (a)-(d) below isolate it by
# satisfying the cosmos/vault storage invariants so the auth check is what fires.


def _deployed_custom_tools(**overrides) -> Settings:
    base = dict(
        env="dev",
        custom_tools_enabled=True,
        session_store="cosmos",
        cosmos_endpoint="https://cosmos.example/",
        custom_tools_secret_vault_uri="https://vault.example.net/",
    )
    base.update(overrides)
    return _settings(**base)


def test_enabled_dev_auth_deployed_is_rejected():
    # (a) enabled + dev auth + deployed -> raises, even with allow_dev_auth=True
    # (the spoofable-identity case the owner wants closed for custom tools).
    s = _deployed_custom_tools(auth_provider="dev", allow_dev_auth=True)
    with pytest.raises(RuntimeError, match="real authentication"):
        s.validate_runtime()


def test_enabled_entra_auth_deployed_is_allowed():
    # (b) enabled + entra auth + deployed -> ok.
    s = _deployed_custom_tools(
        auth_provider="entra",
        entra_tenant_id="t1",
        entra_audience="api://ai4ia",
    )
    s.validate_runtime()  # no raise


def test_enabled_dev_auth_local_is_allowed():
    # (c) enabled + dev auth + local -> ok (local is exempt; in-memory stores fine).
    _settings(custom_tools_enabled=True, auth_provider="dev").validate_runtime()


def test_disabled_dev_auth_deployed_is_allowed():
    # (d) disabled + dev auth + deployed -> ok (guard is gated on custom tools).
    s = _settings(env="dev", custom_tools_enabled=False, auth_provider="dev")
    s.validate_runtime()  # no raise
