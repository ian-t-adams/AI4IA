import pytest

from ai4ia_api.auth.base import AuthCredentials
from ai4ia_api.auth.dev import DevAuthProvider
from ai4ia_api.config import AuthProviderKind, Environment
from ai4ia_api.auth.factory import build_auth_provider
from tests.conftest import make_settings


def _provider(allow_header_override=True):
    return DevAuthProvider(
        default_sub="dev-user",
        default_name="Dev User",
        default_email="dev@example.com",
        allow_header_override=allow_header_override,
    )


async def test_dev_provider_returns_default_user():
    user = await _provider().authenticate(AuthCredentials())
    assert user.subject == "dev-user"
    assert user.provider == "dev"
    assert user.internal_user_id


async def test_dev_header_override_changes_identity():
    provider = _provider()
    a = await provider.authenticate(AuthCredentials())
    b = await provider.authenticate(AuthCredentials(headers={"X-Dev-User": "alice"}))
    assert a.internal_user_id != b.internal_user_id
    assert b.subject == "alice"


async def test_dev_header_ignored_when_override_disabled():
    provider = _provider(allow_header_override=False)
    user = await provider.authenticate(AuthCredentials(headers={"X-Dev-User": "alice"}))
    assert user.subject == "dev-user"


def test_factory_rejects_dev_auth_outside_local():
    settings = make_settings(env=Environment.dev, allow_dev_auth=False)
    with pytest.raises(RuntimeError):
        settings.validate_runtime()


def test_factory_builds_dev_provider_when_allowed():
    settings = make_settings()
    provider = build_auth_provider(settings)
    assert isinstance(provider, DevAuthProvider)
    assert settings.auth_provider == AuthProviderKind.dev


def test_entra_requires_tenant_and_audience():
    settings = make_settings(auth_provider="entra")
    with pytest.raises(RuntimeError):
        settings.validate_runtime()


def test_prod_gateway_must_require_auth():
    settings = make_settings(
        env="prod",
        auth_provider="entra",
        entra_tenant_id="t1",
        entra_audience="api://ai4ia",
        model_gateway_auth_mode="none",
    )
    with pytest.raises(RuntimeError):
        settings.validate_runtime()
