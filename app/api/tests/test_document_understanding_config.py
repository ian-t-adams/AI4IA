"""Config fail-closed checks for document understanding (Phase 11A).

The feature is durable cross-session storage, so enabling it in a deployed env
with the in-memory session store would silently drop every manifest on restart.
validate_runtime() must reject that combination while leaving local/dev and the
default-OFF posture untouched.
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
    assert s.document_understanding_enabled is False
    s.validate_runtime()  # no raise


def test_enabled_local_with_memory_is_allowed():
    # Local dev may use the in-memory library freely.
    _settings(document_understanding_enabled=True).validate_runtime()


def test_enabled_deployed_with_memory_store_is_rejected():
    s = _settings(
        env="dev",
        allow_dev_auth=True,
        document_understanding_enabled=True,
        session_store="memory",
    )
    with pytest.raises(RuntimeError, match="cosmos session store"):
        s.validate_runtime()


def test_enabled_deployed_with_cosmos_store_is_allowed():
    s = _settings(
        env="dev",
        allow_dev_auth=True,
        document_understanding_enabled=True,
        session_store="cosmos",
        cosmos_endpoint="https://cosmos.example/",
    )
    s.validate_runtime()  # no raise
