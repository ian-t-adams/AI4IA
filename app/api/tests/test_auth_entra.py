"""Unit tests for EntraAuthProvider's RS256 token validation.

These mint real RS256-signed JWTs against an in-test RSA keypair and pre-seed
the provider's JWKS cache (no network). The focus is the audience-hardening:
a v2.0 access token's ``aud`` is the bare client-ID GUID, while ``api://<guid>``
is the v1 shape — both are canonical IDs for the same app registration and must
be accepted regardless of which form was configured.
"""
from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from ai4ia_api.auth.base import AuthCredentials, AuthError
from ai4ia_api.auth.entra import EntraAuthProvider

TENANT = "6df60a0a-8c74-433a-9d69-513af272d8d4"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
KID = "test-key-1"
BARE_GUID = "97ce0344-1bef-4ab6-94bb-be938e0c40d9"
API_URI = f"api://{BARE_GUID}"
OTHER_GUID = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@pytest.fixture(scope="module")
def keypair():
    """An RSA keypair plus a JWKS dict shaped like the Entra discovery endpoint."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = KID
    public_jwk["use"] = "sig"
    jwks = {"keys": [public_jwk]}
    return private_pem, jwks


def _mint(private_pem: str, *, aud, tid: str = TENANT, exp_delta: int = 3600) -> str:
    now = int(time.time())
    claims = {
        "aud": aud,
        "iss": ISSUER,
        "tid": tid,
        "iat": now,
        "exp": now + exp_delta,
        "oid": "00000000-0000-0000-0000-000000000001",
        "sub": "subject-123",
    }
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": KID})


def _provider(jwks: dict, *, audience: str, allowed_tenants=None) -> EntraAuthProvider:
    provider = EntraAuthProvider(
        audience=audience,
        allowed_tenants=allowed_tenants or [TENANT],
    )
    # Pre-seed the per-tenant JWKS cache so authenticate() never hits the network.
    provider._jwks_cache[TENANT] = (time.time() + 3600, jwks)
    return provider


async def test_bare_aud_with_bare_audience_ok(keypair):
    private_pem, jwks = keypair
    provider = _provider(jwks, audience=BARE_GUID)
    token = _mint(private_pem, aud=BARE_GUID)
    user = await provider.authenticate(AuthCredentials(token=token))
    assert user.subject == "00000000-0000-0000-0000-000000000001"
    assert user.provider == "entra"
    assert user.tenant_id == TENANT
    assert user.internal_user_id


async def test_bare_aud_with_api_uri_audience_ok(keypair):
    """The exact regression that caused the production 401: v2 token (bare-GUID
    aud) against an API configured with the ``api://`` form."""
    private_pem, jwks = keypair
    provider = _provider(jwks, audience=API_URI)
    token = _mint(private_pem, aud=BARE_GUID)
    user = await provider.authenticate(AuthCredentials(token=token))
    assert user.subject == "00000000-0000-0000-0000-000000000001"


async def test_api_uri_aud_with_bare_audience_ok(keypair):
    private_pem, jwks = keypair
    provider = _provider(jwks, audience=BARE_GUID)
    token = _mint(private_pem, aud=API_URI)
    user = await provider.authenticate(AuthCredentials(token=token))
    assert user.subject == "00000000-0000-0000-0000-000000000001"


async def test_api_uri_aud_with_api_uri_audience_ok(keypair):
    private_pem, jwks = keypair
    provider = _provider(jwks, audience=API_URI)
    token = _mint(private_pem, aud=API_URI)
    user = await provider.authenticate(AuthCredentials(token=token))
    assert user.subject == "00000000-0000-0000-0000-000000000001"


async def test_aud_as_list_containing_accepted_value_ok(keypair):
    private_pem, jwks = keypair
    provider = _provider(jwks, audience=BARE_GUID)
    token = _mint(private_pem, aud=[OTHER_GUID, BARE_GUID])
    user = await provider.authenticate(AuthCredentials(token=token))
    assert user.subject == "00000000-0000-0000-0000-000000000001"


async def test_wrong_audience_rejected(keypair):
    private_pem, jwks = keypair
    provider = _provider(jwks, audience=BARE_GUID)
    token = _mint(private_pem, aud=OTHER_GUID)
    with pytest.raises(AuthError) as exc:
        await provider.authenticate(AuthCredentials(token=token))
    assert "Invalid audience" in str(exc.value)


async def test_missing_audience_rejected(keypair):
    private_pem, jwks = keypair
    provider = _provider(jwks, audience=BARE_GUID)
    token = _mint(private_pem, aud=None)
    with pytest.raises(AuthError) as exc:
        await provider.authenticate(AuthCredentials(token=token))
    assert "Invalid audience" in str(exc.value)


async def test_tenant_not_allowed_rejected(keypair):
    private_pem, jwks = keypair
    provider = _provider(jwks, audience=BARE_GUID)
    token = _mint(private_pem, aud=BARE_GUID, tid=OTHER_TENANT)
    with pytest.raises(AuthError) as exc:
        await provider.authenticate(AuthCredentials(token=token))
    assert "tenant" in str(exc.value).lower()


async def test_expired_token_rejected(keypair):
    private_pem, jwks = keypair
    provider = _provider(jwks, audience=BARE_GUID)
    token = _mint(private_pem, aud=BARE_GUID, exp_delta=-3600)
    with pytest.raises(AuthError) as exc:
        await provider.authenticate(AuthCredentials(token=token))
    assert "Token validation failed" in str(exc.value)
