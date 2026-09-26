"""AI4IA_PHOTO_AVATARS_ENABLED fails closed without its prerequisites (paired)."""
from __future__ import annotations

import pytest

from tests.conftest import make_settings

PROD_READY = dict(
    env="prod",
    model_gateway_url="https://proxy.test/openai",
    model_gateway_allowed_hosts="proxy.test",
    model_gateway_auth_mode="api_key",
    model_gateway_api_key="test-proxy-key",
    model_gateway_api_key_header="S7P-KEY",
    auth_provider="entra",
    entra_tenant_id="tenant",
    entra_audience="api://audience",
    session_store="cosmos",
    cosmos_endpoint="https://cosmos.test:443/",
    photo_avatars_enabled=True,
    photo_avatar_blob_account_url="https://media.blob.core.windows.net",
)


def test_flag_and_limits_default_off_and_read_the_environment(monkeypatch):
    settings = make_settings()
    assert settings.photo_avatars_enabled is False
    assert (settings.photo_avatar_max_per_user, settings.photo_avatar_max_creations_per_day) == (5, 5)
    assert settings.photo_avatar_blob_container == "avatars"
    monkeypatch.setenv("AI4IA_PHOTO_AVATARS_ENABLED", "true")
    monkeypatch.setenv("AI4IA_PHOTO_AVATAR_MAX_PER_USER", "3")
    settings = make_settings()
    assert settings.photo_avatars_enabled is True and settings.photo_avatar_max_per_user == 3


def test_a_complete_deployed_configuration_validates():
    make_settings(**PROD_READY).validate_runtime()


@pytest.mark.parametrize("override, message", [
    ({"photo_avatar_blob_account_url": None}, "PHOTO_AVATAR_BLOB_ACCOUNT_URL"),
    ({"photo_avatar_blob_account_url": "http://media.blob.core.windows.net"}, "PHOTO_AVATAR_BLOB_ACCOUNT_URL"),
    ({"session_store": "memory"}, "Cosmos"),
    ({"auth_provider": "dev", "allow_dev_auth": True}, "Entra"),
    ({"usage_metering_enabled": False, "entitlements_enabled": False}, "usage metering"),
    ({"photo_avatar_max_per_user": 0}, "MAX_PER_USER"),
    ({"photo_avatar_max_creations_per_day": 51}, "MAX_CREATIONS_PER_DAY"),
    ({"data_residency": "eu"}, "processes avatars in eastus2"),
])
def test_each_missing_prerequisite_refuses_startup_only_while_enabled(override, message):
    with pytest.raises(RuntimeError, match=message):
        make_settings(**{**PROD_READY, **override}).validate_runtime()
    # Control: the identical configuration with the feature off still starts.
    if override.get("data_residency") == "eu":
        return  # a residency policy has independent requirements of its own
    make_settings(**{**PROD_READY, **override, "photo_avatars_enabled": False}).validate_runtime()


@pytest.mark.parametrize("residency, ok", [("global", True), ("zonal", True), ("us", True), ("eu", False)])
def test_local_validation_checks_limits_and_residency_but_not_durable_storage(residency, ok):
    settings = make_settings(photo_avatars_enabled=True, data_residency=residency)
    if ok:
        settings.validate_runtime()
    else:
        with pytest.raises(RuntimeError, match="eastus2"):
            settings.validate_runtime()
