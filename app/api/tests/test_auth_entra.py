"""Unit tests for EntraAuthProvider's RS256 token validation.

These mint real RS256-signed JWTs against an in-test RSA keypair and pre-seed
the provider's JWKS cache (no network). The focus is the audience-hardening:
a v2.0 access token's ``aud`` is the bare client-ID GUID, while ``api://<guid>``
is the v1 shape — both are canonical IDs for the same app registration and must
be accepted regardless of which form was configured.
"""
from __future__ import annotations

import asyncio
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
ROTATED_KID = "test-key-2"
BARE_GUID = "97ce0344-1bef-4ab6-94bb-be938e0c40d9"
API_URI = f"api://{BARE_GUID}"
OTHER_GUID = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@pytest.fixture(scope="module")
def keypair():
    """An RSA keypair plus a JWKS dict shaped like the Entra discovery endpoint."""
    return _new_keypair(KID)


def _new_keypair(kid: str):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = kid
    public_jwk["use"] = "sig"
    jwks = {"keys": [public_jwk]}
    return private_pem, jwks


def _mint(
    private_pem: str,
    *,
    aud,
    tid: str = TENANT,
    exp_delta: int = 3600,
    kid: str = KID,
) -> str:
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
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


class _JwksResponse:
    def __init__(self, jwks: dict) -> None:
        self._jwks = jwks

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._jwks


class _JwksHttpClient:
    def __init__(self, jwks: dict) -> None:
        self._jwks = jwks
        self.calls = 0

    async def get(self, url: str) -> _JwksResponse:
        self.calls += 1
        await asyncio.sleep(0)
        return _JwksResponse(self._jwks)


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


async def test_cached_known_key_does_not_refetch(keypair):
    private_pem, jwks = keypair
    http = _JwksHttpClient({"keys": []})
    provider = EntraAuthProvider(
        audience=BARE_GUID,
        allowed_tenants=[TENANT],
        http_client=http,
    )
    provider._jwks_cache[TENANT] = (time.time() + 3600, jwks)

    user = await provider.authenticate(
        AuthCredentials(token=_mint(private_pem, aud=BARE_GUID))
    )

    assert user.tenant_id == TENANT
    assert http.calls == 0


async def test_unknown_kid_refetches_once_and_accepts_rotated_key(keypair):
    _, stale_jwks = keypair
    rotated_private, rotated_jwks = _new_keypair(ROTATED_KID)
    http = _JwksHttpClient(rotated_jwks)
    provider = EntraAuthProvider(
        audience=BARE_GUID,
        allowed_tenants=[TENANT],
        http_client=http,
    )
    provider._jwks_cache[TENANT] = (time.time() + 3600, stale_jwks)
    token = _mint(rotated_private, aud=BARE_GUID, kid=ROTATED_KID)

    user = await provider.authenticate(AuthCredentials(token=token))

    assert user.tenant_id == TENANT
    assert http.calls == 1


async def test_unknown_kid_after_refresh_remains_fail_closed(keypair):
    _, stale_jwks = keypair
    unknown_private, _ = _new_keypair(ROTATED_KID)
    http = _JwksHttpClient(stale_jwks)
    provider = EntraAuthProvider(
        audience=BARE_GUID,
        allowed_tenants=[TENANT],
        http_client=http,
    )
    provider._jwks_cache[TENANT] = (time.time() + 3600, stale_jwks)
    token = _mint(unknown_private, aud=BARE_GUID, kid=ROTATED_KID)

    with pytest.raises(AuthError, match="no matching signing key"):
        await provider.authenticate(AuthCredentials(token=token))

    assert http.calls == 1


async def test_repeated_unknown_kids_do_not_force_repeated_refreshes(keypair):
    _, stale_jwks = keypair
    http = _JwksHttpClient(stale_jwks)
    provider = EntraAuthProvider(
        audience=BARE_GUID,
        allowed_tenants=[TENANT],
        http_client=http,
    )
    provider._jwks_cache[TENANT] = (time.time() + 3600, stale_jwks)
    unknown_one, _ = _new_keypair("unknown-1")
    unknown_two, _ = _new_keypair("unknown-2")

    for private_key, kid in (
        (unknown_one, "unknown-1"),
        (unknown_two, "unknown-2"),
    ):
        token = _mint(private_key, aud=BARE_GUID, kid=kid)
        with pytest.raises(AuthError, match="no matching signing key"):
            await provider.authenticate(AuthCredentials(token=token))

    assert http.calls == 1


async def test_concurrent_unknown_kid_rotation_is_single_flight(keypair):
    _, stale_jwks = keypair
    rotated_private, rotated_jwks = _new_keypair(ROTATED_KID)
    http = _JwksHttpClient(rotated_jwks)
    provider = EntraAuthProvider(
        audience=BARE_GUID,
        allowed_tenants=[TENANT],
        http_client=http,
    )
    provider._jwks_cache[TENANT] = (time.time() + 3600, stale_jwks)
    token = _mint(rotated_private, aud=BARE_GUID, kid=ROTATED_KID)

    users = await asyncio.gather(
        *(
            provider.authenticate(AuthCredentials(token=token))
            for _ in range(20)
        )
    )

    assert len(users) == 20
    assert all(user.tenant_id == TENANT for user in users)
    assert http.calls == 1
