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


def test_inline_compute_api_key_mode_without_key_is_rejected():
    with pytest.raises(RuntimeError, match="AI4IA_CODE_INTERPRETER_API_KEY"):
        _settings(
            inline_document_compute_enabled=True,
            code_interpreter_base_url="https://foundry.example/",
            code_interpreter_model="gpt-5.4-mini",
            code_interpreter_auth_mode="api_key",
            code_interpreter_api_key=None,
        ).validate_runtime()


def test_inline_compute_deployed_requires_cosmos_and_blob_storage():
    base = dict(
        env="dev",
        allow_dev_auth=True,
        inline_document_compute_enabled=True,
        code_interpreter_base_url="https://gateway.example/code-interpreter",
        code_interpreter_model="gpt-5.4-mini",
        code_interpreter_auth_mode="api_key",
        code_interpreter_api_key="code-interpreter-key",
    )
    with pytest.raises(RuntimeError, match="SESSION_STORE=cosmos"):
        _settings(
            **base,
            session_store="memory",
            document_blob_account_url="https://acct.blob.core.windows.net",
        ).validate_runtime()
    with pytest.raises(RuntimeError, match="DOCUMENT_BLOB_ACCOUNT_URL"):
        _settings(
            **base,
            session_store="cosmos",
            cosmos_endpoint="https://cosmos.example/",
        ).validate_runtime()

    _settings(
        **base,
        session_store="cosmos",
        cosmos_endpoint="https://cosmos.example/",
        document_blob_account_url="https://acct.blob.core.windows.net",
    ).validate_runtime()


def test_deployed_code_interpreter_requires_scoped_apim_posture():
    base = dict(
        env="dev",
        allow_dev_auth=True,
        inline_document_compute_enabled=True,
        code_interpreter_model="gpt-5.4-mini",
        session_store="cosmos",
        cosmos_endpoint="https://cosmos.example/",
        document_blob_account_url="https://acct.blob.core.windows.net",
    )
    with pytest.raises(RuntimeError, match="dedicated HTTPS APIM"):
        _settings(
            **base,
            code_interpreter_base_url="https://foundry.services.ai.azure.com",
            code_interpreter_auth_mode="bearer",
        ).validate_runtime()
    with pytest.raises(RuntimeError, match="dedicated HTTPS APIM"):
        _settings(
            **base,
            code_interpreter_base_url="https://gateway.example/wrong-path",
            code_interpreter_auth_mode="api_key",
            code_interpreter_api_key="code-interpreter-key",
        ).validate_runtime()

    _settings(
        **base,
        code_interpreter_base_url="https://gateway.example/code-interpreter",
        code_interpreter_auth_mode="api_key",
        code_interpreter_api_key="code-interpreter-key",
    ).validate_runtime()


@pytest.mark.parametrize(
    "field",
    (
        "model_gateway_api_key",
        "realtime_gateway_api_key",
        "speech_voice_live_gateway_api_key",
        "official_mcp_subscription_key",
    ),
)
def test_code_interpreter_apim_key_cannot_be_reused(field: str):
    overrides = {
        "env": "dev",
        "allow_dev_auth": True,
        "inline_document_compute_enabled": True,
        "code_interpreter_base_url": "https://gateway.example/code-interpreter",
        "code_interpreter_model": "gpt-5.4-mini",
        "code_interpreter_auth_mode": "api_key",
        "code_interpreter_api_key": "shared-key",
        "session_store": "cosmos",
        "cosmos_endpoint": "https://cosmos.example/",
        "document_blob_account_url": "https://acct.blob.core.windows.net",
        field: "shared-key",
    }
    with pytest.raises(RuntimeError, match="distinct API-scoped APIM key"):
        _settings(**overrides).validate_runtime()


def test_automatic_cu_api_version_cannot_be_changed_to_preview():
    with pytest.raises(RuntimeError, match="must remain 2025-11-01"):
        _settings(
            document_understanding_enabled=True,
            cu_api_version="2026-06-01-preview",
        ).validate_runtime()


def test_cu_preview_requires_document_understanding():
    with pytest.raises(RuntimeError, match="CU_PREVIEW_ENABLED"):
        _settings(cu_preview_enabled=True).validate_runtime()


def test_cu_agentic_analyzer_requires_preview_and_valid_id():
    with pytest.raises(RuntimeError, match="CU_PREVIEW_ENABLED"):
        _settings(
            document_understanding_enabled=True,
            cu_agentic_analyzer_id="agentic.contract",
        ).validate_runtime()
    with pytest.raises(RuntimeError, match="valid Content Understanding"):
        _settings(
            document_understanding_enabled=True,
            cu_preview_enabled=True,
            cu_agentic_analyzer_id="../bad",
        ).validate_runtime()


def test_cu_agentic_analyzer_is_allowed_when_preview_is_enabled():
    _settings(
        document_understanding_enabled=True,
        cu_preview_enabled=True,
        cu_agentic_analyzer_id="agentic.contract",
    ).validate_runtime()

# --- CU endpoint shape ----------------------------------------------------------
#
# CU receives raw document bytes and a Cognitive Services access token, so the
# endpoint is a confidentiality boundary. Deployed startup must reject shapes
# that would send both somewhere unintended.

_DEPLOYED = dict(
    env="dev",
    allow_dev_auth=True,
    document_understanding_enabled=True,
    session_store="cosmos",
    cosmos_endpoint="https://cosmos.example/",
    document_blob_account_url="https://acct.blob.core.windows.net",
)


def test_deployed_cu_base_url_must_be_https_without_credentials():
    for bad in (
        "http://cu.example",                      # plaintext
        "https://user:pw@cu.example",             # embedded credentials
        "https://cu.example?sneak=1",             # query
        "https://cu.example#frag",                # fragment
        "not-a-url",                              # no scheme/host
    ):
        with pytest.raises(RuntimeError, match="AI4IA_CU_BASE_URL"):
            _settings(**_DEPLOYED, cu_base_url=bad).validate_runtime()


def test_deployed_cu_base_url_https_is_accepted():
    """Control: the guard above rejects *shapes*, not every deployed CU URL."""
    _settings(
        **_DEPLOYED, cu_base_url="https://cu.example/"
    ).validate_runtime()  # no raise


def test_local_cu_base_url_shape_is_not_enforced():
    """Local/dev may point CU at a loopback emulator over plain http."""
    _settings(
        document_understanding_enabled=True,
        cu_base_url="http://localhost:5000",
    ).validate_runtime()  # no raise