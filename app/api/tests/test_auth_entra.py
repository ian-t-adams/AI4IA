"""Unit tests for EntraAuthProvider's RS256 token validation.

These mint synthetic RS256-signed JWTs against an in-test RSA keypair and pre-seed
the provider's JWKS cache (no network). General authentication accepts its
configured canonical audience forms; strict pilot admission requires the v2.0
bare client-ID GUID and a configured delegated-user client.
"""
from __future__ import annotations

import asyncio
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jwt.algorithms import RSAAlgorithm
from starlette.requests import Request

from ai4ia_api.auth.base import AuthCredentials, AuthError
from ai4ia_api.auth.dependencies import get_current_user
from ai4ia_api.auth.entra import EntraAuthProvider, EntraPilotAdmission

TENANT = "6907d2a4-685a-4aea-92ab-d930217467f1"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
KID = "test-key-1"
ROTATED_KID = "test-key-2"
BARE_GUID = "97ce0344-1bef-4ab6-94bb-be938e0c40d9"
LEGACY_API_URI = f"api://{BARE_GUID}"
OTHER_GUID = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
PILOT_OID = "00000000-0000-0000-0000-000000000002"
REGISTERED_API_APP_ID = "ec20035f-9539-4427-a6d2-b8f3bb2f9b5a"
WEB_CLIENT_ID = "44f04b0b-f858-43df-8406-9c61690a4433"
API_URI = f"api://{REGISTERED_API_APP_ID}"
PILOT_AZP = WEB_CLIENT_ID


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
    claim_overrides: dict | None = None,
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
    if claim_overrides:
        claims.update(claim_overrides)
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


def _pilot_provider(
    jwks: dict, *, audience: str = REGISTERED_API_APP_ID, azp: str = PILOT_AZP
) -> EntraAuthProvider:
    provider = EntraAuthProvider(
        audience=audience,
        allowed_tenants=[TENANT],
        pilot_admission=EntraPilotAdmission(
            tenant_id=TENANT,
            oid=PILOT_OID,
            azp=azp,
        ),
    )
    provider._jwks_cache[TENANT] = (time.time() + 3600, jwks)
    return provider


def _pilot_token(private_pem: str, *, azp: str = PILOT_AZP, **kwargs) -> str:
    overrides = {
        "oid": PILOT_OID,
        "ver": "2.0",
        "azp": azp,
        "scp": "access_as_user",
    }
    overrides.update(kwargs.pop("claim_overrides", {}))
    return _mint(
        private_pem,
        aud=kwargs.pop("aud", REGISTERED_API_APP_ID),
        claim_overrides=overrides,
        **kwargs,
    )


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
    provider = _provider(jwks, audience=LEGACY_API_URI)
    token = _mint(private_pem, aud=BARE_GUID)
    user = await provider.authenticate(AuthCredentials(token=token))
    assert user.subject == "00000000-0000-0000-0000-000000000001"


async def test_api_uri_aud_with_bare_audience_ok(keypair):
    private_pem, jwks = keypair
    provider = _provider(jwks, audience=BARE_GUID)
    token = _mint(private_pem, aud=LEGACY_API_URI)
    user = await provider.authenticate(AuthCredentials(token=token))
    assert user.subject == "00000000-0000-0000-0000-000000000001"


async def test_api_uri_aud_with_api_uri_audience_ok(keypair):
    private_pem, jwks = keypair
    provider = _provider(jwks, audience=LEGACY_API_URI)
    token = _mint(private_pem, aud=LEGACY_API_URI)
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


async def test_pilot_accepts_v2_guid_audience_and_preserves_verified_claims(keypair):
    private_pem, jwks = keypair
    provider = _pilot_provider(jwks, audience=API_URI)

    user = await provider.authenticate(
        AuthCredentials(token=_pilot_token(private_pem))
    )

    assert user.subject == PILOT_OID
    assert user.oid == PILOT_OID
    assert user.tid == TENANT
    assert user.iss == ISSUER
    assert user.ver == "2.0"
    assert user.azp == PILOT_AZP
    assert user.claims["scp"] == "access_as_user"
    assert not {"oid", "tid", "iss", "ver", "azp"} & user.model_dump().keys()


async def test_pilot_accepts_a_separately_configured_spa_client_id(keypair):
    private_pem, jwks = keypair
    pilot_spa_client_id = "f0000000-0000-4000-8000-000000000001"
    provider = _pilot_provider(jwks, azp=pilot_spa_client_id)

    user = await provider.authenticate(
        AuthCredentials(
            token=_pilot_token(private_pem, azp=pilot_spa_client_id)
        )
    )

    assert user.oid == PILOT_OID
    assert user.azp == pilot_spa_client_id


async def test_nonpilot_keeps_legacy_sub_fallback(keypair):
    private_pem, jwks = keypair
    provider = _provider(jwks, audience=BARE_GUID)

    user = await provider.authenticate(
        AuthCredentials(
            token=_mint(
                private_pem,
                aud=BARE_GUID,
                claim_overrides={"oid": None, "ver": None, "azp": None},
            )
        )
    )

    assert user.subject == "subject-123"


@pytest.mark.parametrize(
    ("audience", "claims"),
    [
        (API_URI, {}),
        (OTHER_GUID, {}),
        (123, {}),
        ([{"nested": "invalid"}, REGISTERED_API_APP_ID], {}),
        ([REGISTERED_API_APP_ID], {}),
        (REGISTERED_API_APP_ID, {"oid": None}),
        (REGISTERED_API_APP_ID, {"oid": WEB_CLIENT_ID}),
        (REGISTERED_API_APP_ID, {"oid": REGISTERED_API_APP_ID}),
        (REGISTERED_API_APP_ID, {"oid": OTHER_GUID}),
        (REGISTERED_API_APP_ID, {"tid": OTHER_TENANT}),
        (REGISTERED_API_APP_ID, {"iss": f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0"}),
        (REGISTERED_API_APP_ID, {"ver": None}),
        (REGISTERED_API_APP_ID, {"ver": "1.0"}),
        (REGISTERED_API_APP_ID, {"azp": None}),
        (REGISTERED_API_APP_ID, {"azp": OTHER_GUID}),
        (REGISTERED_API_APP_ID, {"scp": None, "roles": ["access_as_user"]}),
        (REGISTERED_API_APP_ID, {"scp": "", "roles": ["access_as_user"]}),
        (REGISTERED_API_APP_ID, {"scp": "access_as_user_x"}),
    ],
)
async def test_pilot_rejects_claims_outside_verified_delegated_contract(
    keypair, audience, claims
):
    private_pem, jwks = keypair
    provider = _pilot_provider(jwks, audience=API_URI)

    with pytest.raises(AuthError):
        await provider.authenticate(
            AuthCredentials(
                token=_pilot_token(
                    private_pem,
                    aud=audience,
                    claim_overrides=claims,
                )
            )
        )


@pytest.mark.parametrize(
    "scope",
    ["access_as_user", "openid access_as_user"],
)
async def test_pilot_accepts_exact_scope_token(keypair, scope):
    private_pem, jwks = keypair
    provider = _pilot_provider(jwks)

    user = await provider.authenticate(
        AuthCredentials(
            token=_pilot_token(
                private_pem,
                claim_overrides={"scp": scope},
            )
        )
    )

    assert user.oid == PILOT_OID


async def test_pilot_rejects_token_with_sub_only(keypair):
    private_pem, jwks = keypair
    provider = _pilot_provider(jwks)

    with pytest.raises(AuthError, match="pilot identity contract"):
        await provider.authenticate(
            AuthCredentials(
                token=_pilot_token(private_pem, claim_overrides={"oid": None})
            )
        )


async def test_pilot_auth_failure_precedes_downstream_admission_side_effects(
    keypair, monkeypatch
):
    import ai4ia_api.auth.dependencies as dependencies

    private_pem, jwks = keypair
    side_effects = []
    app = FastAPI()
    app.state.auth_provider = _pilot_provider(jwks)
    app.state.hard_quota = object()
    app.state.policy = object()
    app.state.user_directory = type(
        "Directory",
        (),
        {"capture": lambda *_args: side_effects.append("directory")},
    )()
    app.state.settings = type("Settings", (), {"proxy_priorities_enabled": True})()
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/chat",
        "headers": [],
        "app": app,
    })
    monkeypatch.setattr(
        dependencies,
        "set_admission_owner",
        lambda *_args: side_effects.append("quota"),
    )
    monkeypatch.setattr(
        dependencies,
        "bind_authenticated",
        lambda *_args: side_effects.append("policy"),
    )

    async def authorize_http_operation_with_recording(*_args):
        side_effects.append("authorization")

    monkeypatch.setattr(
        dependencies,
        "authorize_http_operation",
        authorize_http_operation_with_recording,
    )
    monkeypatch.setattr(
        dependencies,
        "resolve_priority",
        lambda *_args: side_effects.append("priority"),
    )
    monkeypatch.setattr(
        dependencies,
        "set_request_priority",
        lambda *_args: side_effects.append("request priority"),
    )
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=_pilot_token(
            private_pem,
            claim_overrides={"oid": OTHER_GUID},
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await get_current_user(request, credentials)

    assert exc.value.status_code == 401
    assert side_effects == []

    accepted_credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=_pilot_token(
            private_pem,
            claim_overrides={"oid": PILOT_OID},
        ),
    )
    user = await get_current_user(request, accepted_credentials)

    assert user.oid == PILOT_OID
    assert side_effects == [
        "quota",
        "policy",
        "authorization",
        "directory",
        "priority",
        "request priority",
    ]


async def test_pilot_configuration_requires_guid_audience(keypair):
    _, jwks = keypair
    with pytest.raises(ValueError, match="API client-ID GUID"):
        _pilot_provider(jwks, audience="https://api.example.invalid")


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
