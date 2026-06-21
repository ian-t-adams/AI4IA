"""Config fail-closed checks for document understanding.

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
        cu_base_url="https://cu.example/",
        document_blob_account_url="https://acct.blob.core.windows.net",
    )
    s.validate_runtime()  # no raise


def test_enabled_deployed_without_cu_or_blob_is_rejected():
    # The 11B ingest path needs both the CU endpoint and the blob account in a
    # deployed env; enabling without them must fail closed.
    base = dict(
        env="dev",
        allow_dev_auth=True,
        document_understanding_enabled=True,
        session_store="cosmos",
        cosmos_endpoint="https://cosmos.example/",
    )
    with pytest.raises(RuntimeError, match="AI4IA_CU_BASE_URL"):
        _settings(**base, document_blob_account_url="https://acct.blob.core.windows.net").validate_runtime()
    with pytest.raises(RuntimeError, match="AI4IA_CU_BASE_URL"):
        _settings(**base, cu_base_url="https://cu.example/").validate_runtime()


def test_enabled_local_without_cu_or_blob_is_allowed():
    # Local/dev runs with an in-memory blob store and CU disabled.
    _settings(document_understanding_enabled=True).validate_runtime()


def test_cu_analyzer_for_modality_maps_each_class():
    s = _settings()
    assert s.cu_analyzer_for_modality("document") == s.cu_document_analyzer
    assert s.cu_analyzer_for_modality("text") == s.cu_document_analyzer
    assert s.cu_analyzer_for_modality("image") == s.cu_image_analyzer
    assert s.cu_analyzer_for_modality("audio") == s.cu_audio_analyzer
    assert s.cu_analyzer_for_modality("video") == s.cu_video_analyzer
    # Unknown modality falls back to the document analyzer.
    assert s.cu_analyzer_for_modality("other") == s.cu_document_analyzer


def test_cu_api_key_mode_without_key_is_rejected():
    # api_key auth with no key would silently send no auth header and fail at the
    # first CU call. Fail loud at startup instead — in any env once CU is wired.
    with pytest.raises(RuntimeError, match="AI4IA_CU_API_KEY"):
        _settings(
            document_understanding_enabled=True,
            cu_base_url="https://cu.example/",
            cu_auth_mode="api_key",
            cu_api_key=None,
        ).validate_runtime()


def test_cu_api_key_mode_with_key_is_allowed():
    _settings(
        document_understanding_enabled=True,
        cu_base_url="https://cu.example/",
        cu_auth_mode="api_key",
        cu_api_key="secret",
    ).validate_runtime()  # no raise


def test_cu_bearer_mode_without_key_is_allowed():
    # bearer mode uses managed identity when no static key is set — valid.
    _settings(
        document_understanding_enabled=True,
        cu_base_url="https://cu.example/",
        cu_auth_mode="bearer",
        cu_api_key=None,
    ).validate_runtime()  # no raise
