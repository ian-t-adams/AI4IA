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


@pytest.mark.parametrize("environment,store", [("local", "cosmos"), ("dev", "memory"), ("dev", "cosmos")])
def test_hard_mode_cannot_activate_durable_state(environment, store):
    settings = make_settings(
        env=environment, session_store=store, cosmos_endpoint="https://cosmos.test",
        model_gateway_auth_mode="api_key", model_gateway_api_key="test-key",
        model_gateway_api_key_header="S7P-KEY",
        model_gateway_url="https://proxy.test/openai", model_gateway_allowed_hosts="proxy.test",
        hard_quota_enabled=True,
    )
    with pytest.raises(RuntimeError, match="no approved durable activation"):
        settings.validate_runtime()
    # Identical valid deployed/local configuration, only the hard flag changes.
    settings.hard_quota_enabled = False
    settings.validate_runtime()
