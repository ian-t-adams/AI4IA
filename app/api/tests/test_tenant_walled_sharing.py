"""The `public` visibility must not silently become cross-tenant.

`library/access.py` grants `Visibility.public` to any authenticated caller without
comparing tenant identity. That is safe only because the app authenticates against
a single tenant, which makes "public" mean "tenant-public" -- an assumption written
in that module's docstring and, until now, enforced nowhere.

`AI4IA_ENTRA_ALLOWED_TENANTS` is a comma-separated list. Adding a second tenant is a
one-variable change that would retroactively convert every existing `public`
document into cross-tenant readable, with no migration and no signal, because those
documents were shared under the old meaning of the word.

That is audit finding P1-10. It stays latent only because nobody has a second tenant
yet, which makes it exactly the kind of finding that gets rediscovered as an incident
rather than as a task. The guard turns it into a startup refusal.
"""

from __future__ import annotations

import pytest

from ai4ia_api.config import AuthProviderKind, Environment, Settings


def _entra_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": Environment.prod,
        "auth_provider": AuthProviderKind.entra,
        "entra_tenant_id": "11111111-1111-1111-1111-111111111111",
        "entra_audience": "api://ai4ia",
        "model_gateway_auth_mode": "api_key",
        "model_gateway_api_key": "x" * 32,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_single_tenant_starts_normally() -> None:
    """The supported configuration today. Guards against a vacuous test below."""
    settings = _entra_settings()
    assert len(settings.allowed_tenants) == 1
    settings.validate_runtime()


def test_explicit_single_allowed_tenant_also_starts() -> None:
    settings = _entra_settings(
        entra_allowed_tenants="11111111-1111-1111-1111-111111111111"
    )
    assert len(settings.allowed_tenants) == 1
    settings.validate_runtime()


def test_second_tenant_refuses_to_start() -> None:
    """The finding, enforced.

    Failing here is the point: a `public` document shared under single-tenant
    semantics must not become readable by another tenant because someone appended
    a GUID to a variable.
    """
    settings = _entra_settings(
        entra_allowed_tenants=(
            "11111111-1111-1111-1111-111111111111,"
            "22222222-2222-2222-2222-222222222222"
        )
    )
    assert len(settings.allowed_tenants) == 2
    with pytest.raises(RuntimeError, match="not tenant-aware"):
        settings.validate_runtime()


def test_whitespace_and_trailing_commas_do_not_fake_a_second_tenant() -> None:
    """`allowed_tenants` splits on commas, so sloppy formatting must not trip the
    guard -- a false positive here blocks a legitimate single-tenant deploy."""
    settings = _entra_settings(
        entra_allowed_tenants=" 11111111-1111-1111-1111-111111111111 , "
    )
    assert len(settings.allowed_tenants) == 1
    settings.validate_runtime()


def test_dev_auth_is_not_subject_to_the_tenant_guard() -> None:
    """Dev auth has no tenant claim at all, so the check does not apply.

    It is gated on the provider rather than left to `allowed_tenants` happening to
    be short, so the reason is explicit rather than incidental.
    """
    settings = Settings(
        env=Environment.local,
        auth_provider=AuthProviderKind.dev,
        entra_allowed_tenants="a,b,c",
    )
    settings.validate_runtime()
