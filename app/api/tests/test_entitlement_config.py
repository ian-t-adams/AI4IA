"""Startup invariants for entitlement enforcement (Settings.validate_runtime)."""
from __future__ import annotations

import pytest

from tests.conftest import make_settings


def test_enforcement_requires_usage_metering():
    # Enabling enforcement while metering is off would let positive budgets
    # silently never trip; the guard must refuse it.
    settings = make_settings(entitlements_enabled=True, usage_metering_enabled=False)
    with pytest.raises(RuntimeError, match="usage metering"):
        settings.validate_runtime()


def test_enforcement_off_with_metering_off_is_allowed():
    settings = make_settings(entitlements_enabled=False, usage_metering_enabled=False)
    settings.validate_runtime()  # must not raise


def test_default_posture_validates():
    # Shipped defaults: both enabled -> valid.
    make_settings().validate_runtime()


def test_hard_mode_default_off_and_explicit_local_mode_requires_metering():
    assert make_settings().hard_quota_enabled is False
    make_settings(hard_quota_enabled=True).validate_runtime()
    with pytest.raises(RuntimeError, match="requires usage metering"):
        make_settings(
            hard_quota_enabled=True, entitlements_enabled=False, usage_metering_enabled=False,
        ).validate_runtime()


DEPLOYED = dict(
    cosmos_endpoint="https://cosmos.test",
    model_gateway_auth_mode="api_key", model_gateway_api_key="test-key",
    model_gateway_api_key_header="S7P-KEY",
    model_gateway_url="https://proxy.test/openai", model_gateway_allowed_hosts="proxy.test",
)
ENTRA = dict(auth_provider="entra", entra_tenant_id="tenant", entra_audience="api://ai4ia")
ROLLOUT = dict(hard_quota_rollout_id="reviewed-request-count-1")


@pytest.mark.parametrize("environment,store,extra,match", [
    ("dev", "memory", {**ENTRA, **ROLLOUT}, "requires the Cosmos store"),
    ("local", "cosmos", {}, "AI4IA_HARD_QUOTA_ROLLOUT_ID"),
    ("dev", "cosmos", ROLLOUT, "requires Entra outside local"),
    ("dev", "cosmos", ENTRA, "AI4IA_HARD_QUOTA_ROLLOUT_ID"),
    ("dev", "cosmos", {**ENTRA, "hard_quota_rollout_id": "-leading-separator"}, "ROLLOUT_ID"),
    ("dev", "cosmos", {**ENTRA, "hard_quota_rollout_id": "a" * 129}, "ROLLOUT_ID"),
    ("dev", "cosmos", {**ENTRA, "hard_quota_rollout_id": "has space"}, "ROLLOUT_ID"),
])
def test_hard_mode_outside_the_local_fake_requires_an_approved_rollout(environment, store, extra, match):
    settings = make_settings(
        env=environment, session_store=store, **DEPLOYED, **extra, hard_quota_enabled=True,
    )
    with pytest.raises(RuntimeError, match=match):
        settings.validate_runtime()
    # Identical valid deployed/local configuration, only the hard flag changes.
    settings.hard_quota_enabled = False
    settings.validate_runtime()


@pytest.mark.parametrize("environment,extra", [("dev", ENTRA), ("local", {})])
def test_hard_mode_configuration_selects_one_exact_rollout_record(environment, extra):
    settings = make_settings(
        env=environment, session_store="cosmos", **DEPLOYED, **extra, **ROLLOUT,
        hard_quota_enabled=True,
    )
    # Configuration only selects the record; startup must still read and validate it.
    settings.validate_runtime()
    assert settings.hard_quota_rollout_id == "reviewed-request-count-1"
    assert make_settings().hard_quota_rollout_id == ""


TOKEN_USD_DEFAULTS = (
    "default_tokens_per_day", "default_cost_per_day_micro_usd",
    "default_tokens_per_month", "default_cost_per_month_micro_usd",
)


@pytest.mark.parametrize("default", TOKEN_USD_DEFAULTS)
def test_hard_mode_refuses_global_default_token_or_usd_caps(default):
    enabled = dict(
        env="dev", session_store="cosmos", **DEPLOYED, **ENTRA, **ROLLOUT, hard_quota_enabled=True,
    )
    # Every owner without an override would inherit this unenforceable cap.
    with pytest.raises(RuntimeError, match="admits request counts only"):
        make_settings(**enabled, **{default: 1000}).validate_runtime()
    # Control: the identical configuration with all four defaults unset passes,
    # as does a request-count default.
    make_settings(**enabled, **{name: None for name in TOKEN_USD_DEFAULTS}).validate_runtime()
    make_settings(**enabled, default_requests_per_minute=30).validate_runtime()
    # Soft mode keeps accepting the same default caps.
    make_settings(**{**enabled, "hard_quota_enabled": False}, **{default: 1000}).validate_runtime()
